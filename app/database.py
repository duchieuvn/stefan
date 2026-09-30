"""Database access: `.env` settings, SQLite / PostgreSQL providers, and the
SFTP sync that keeps the local SQLite copy fresh.

Every provider returns short-lived connections exposing
`execute(query, params) -> iterable cursor`, with `%s` placeholders.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

ENV_PATH = Path(".env")
load_dotenv(ENV_PATH, override=False)


# --- Settings ---

class SettingsError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _env_optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_required(name: str) -> str:
    value = _env_optional(name)
    if not value:
        raise SettingsError(f"Missing required setting in {ENV_PATH}: {name}")
    return value


def _env_secret(name: str) -> str:
    """Like `_env_required`, but keeps surrounding whitespace (passwords)."""
    value = os.getenv(name, "")
    if not value:
        raise SettingsError(f"Missing required setting in {ENV_PATH}: {name}")
    return value


def _env_port(name: str, default: int) -> int:
    raw = _env_optional(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise SettingsError(f"{name} must be an integer") from error
    if not 1 <= value <= 65535:
        raise SettingsError(f"{name} must be between 1 and 65535")
    return value


@dataclass(frozen=True)
class SSHSettings:
    """Credentials for the remote host (used for the SFTP sync and the Postgres tunnel)."""

    host: str
    port: int
    user: str
    password: str
    known_hosts: Path

    @classmethod
    def from_environment(cls) -> SSHSettings:
        known_hosts = Path(_env_required("RTI_SSH_KNOWN_HOSTS")).expanduser()
        if not known_hosts.is_file():
            raise SettingsError(f"RTI_SSH_KNOWN_HOSTS does not exist or is not a file: {known_hosts}")
        return cls(
            host=_env_required("RTI_SSH_HOST"),
            port=_env_port("RTI_SSH_PORT", 22),
            user=_env_required("RTI_SSH_USER"),
            password=_env_secret("RTI_SSH_PASSWORD"),
            known_hosts=known_hosts,
        )


# --- Providers ---

BACKENDS = ("sqlite", "postgres", "pg-tailscale")


class DatabaseProvider(Protocol):
    """Minimal database interface required by the temporal RTI loader."""
    def connect(self) -> Any: ...
    def close(self) -> None: ...


class DatabaseConnectionError(RuntimeError):
    """Raised when the SSH tunnel or PostgreSQL connection cannot be opened."""


# --- SQLite ---

class _SQLiteConnection:
    """Wraps a sqlite3.Connection to translate `%s` placeholders to `?`."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, query: str, params: tuple | None = None):
        return self._connection.execute(query.replace("%s", "?"), params or ())

    def __getattr__(self, name):
        return getattr(self._connection, name)


class SQLiteDatabase:
    def __init__(self, path: Path):
        self.path = path
        if not self.path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        self._ensure_indexes()

    def connect(self) -> _SQLiteConnection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA busy_timeout=5000")
        return _SQLiteConnection(con)

    def _ensure_indexes(self) -> None:
        try:
            with sqlite3.connect(self.path, timeout=10.0) as con:
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_pc_ts ON measurement(pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_link_time ON measurement(sender_id, receiver_id, pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_ok_time ON measurement(ok_match, pc_ts)")
        except sqlite3.OperationalError as e:
            # E.g. a read-only filesystem: queries still work, just slower.
            logging.warning("Could not create SQLite indexes: %s", e)

    def close(self) -> None:
        """No persistent connection to close."""


# --- PostgreSQL ---

class _PostgresConnection:
    """Wraps a psycopg2 connection with the `execute` interface used by the RTI core."""

    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, query: str, params: tuple | None = None):
        cursor = self._connection.cursor()
        cursor.execute(query, params or ())
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _connect_postgres(dsn: str, **kwargs: Any) -> _PostgresConnection:
    try:
        import psycopg2

        return _PostgresConnection(psycopg2.connect(dsn, **kwargs))
    except Exception as error:
        raise DatabaseConnectionError(f"Could not connect to PostgreSQL: {error}") from error


class PostgresDatabase:
    """Connects directly to PostgreSQL (e.g. via its Tailscale address)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @classmethod
    def from_environment(cls) -> PostgresDatabase:
        return cls(_env_required("RTI_TAILSCALE_POSTGRES_DSN"))

    def connect(self) -> _PostgresConnection:
        return _connect_postgres(self.dsn)

    def close(self) -> None:
        """No persistent connection to close."""


class PostgresTunnelDatabase:
    """Owns a persistent SSH forwarder while returning short-lived DB sessions."""

    def __init__(self, dsn: str, ssh: SSHSettings, db_host: str, db_port: int) -> None:
        self.dsn = dsn
        self.ssh = ssh
        self.db_host = db_host
        self.db_port = db_port
        self._tunnel: Any | None = None

    @classmethod
    def from_environment(cls) -> PostgresTunnelDatabase:
        return cls(
            dsn=_env_required("RTI_POSTGRES_DSN"),
            ssh=SSHSettings.from_environment(),
            db_host=_env_required("RTI_DB_HOST"),
            db_port=_env_port("RTI_DB_PORT", 5432),
        )

    def _known_host_key(self) -> Any:
        try:
            import paramiko
        except ImportError as error:
            raise DatabaseConnectionError("paramiko is not installed") from error

        host_keys = paramiko.HostKeys(filename=str(self.ssh.known_hosts))
        entry = host_keys.lookup(self.ssh.host) or host_keys.lookup(f"[{self.ssh.host}]:{self.ssh.port}")
        if not entry:
            raise DatabaseConnectionError(f"No trusted host key for {self.ssh.host} in {self.ssh.known_hosts}")
        return next(iter(entry.values()))

    def _ensure_tunnel(self) -> Any:
        if self._tunnel is not None and self._tunnel.is_active:
            return self._tunnel
        self.close()
        try:
            from sshtunnel import SSHTunnelForwarder

            tunnel = SSHTunnelForwarder(
                (self.ssh.host, self.ssh.port),
                ssh_username=self.ssh.user,
                ssh_password=self.ssh.password,
                ssh_host_key=self._known_host_key(),
                remote_bind_address=(self.db_host, self.db_port),
                local_bind_address=("127.0.0.1", 0),
                set_keepalive=30.0,
            )
            tunnel.start()
        except DatabaseConnectionError:
            raise
        except Exception as error:
            raise DatabaseConnectionError(f"Could not open SSH tunnel: {error}") from error
        self._tunnel = tunnel
        return tunnel

    def connect(self) -> _PostgresConnection:
        tunnel = self._ensure_tunnel()
        try:
            return _connect_postgres(self.dsn, host="127.0.0.1", port=tunnel.local_bind_port)
        except DatabaseConnectionError:
            self.close()  # discard a failed forwarder; the next connect recreates it
            raise

    def close(self) -> None:
        if self._tunnel is None:
            return
        tunnel, self._tunnel = self._tunnel, None
        try:
            tunnel.stop()
        except Exception:
            logging.warning("Failed to stop SSH tunnel.", exc_info=True)


def open_database(backend: str, db_path: Path) -> DatabaseProvider:
    """Create the provider for `backend` (one of BACKENDS)."""
    if backend == "sqlite":
        return SQLiteDatabase(db_path)
    if backend == "postgres":
        return PostgresTunnelDatabase.from_environment()
    if backend == "pg-tailscale":
        return PostgresDatabase.from_environment()
    raise ValueError(f"Unknown database backend: {backend!r} (expected one of {BACKENDS})")


# --- Local SQLite copy (SFTP sync from the remote host) ---

# e.g. reduced_data_2026-08-27_23-06-00.db
DB_FILENAME_RE = re.compile(r"^reduced_data_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.db$")
DB_FILENAME_TS_FORMAT = "%Y-%m-%d_%H-%M-%S"


class RemoteSyncError(RuntimeError):
    """Raised when the remote SQLite database cannot be fetched."""


def _find_latest_local_db(db_dir: Path) -> tuple[Path, datetime] | None:
    """Returns (path, timestamp) of the newest timestamped local db file, if any."""
    candidates = [
        (p, datetime.strptime(m.group(1), DB_FILENAME_TS_FORMAT))
        for p in db_dir.glob("reduced_data_*.db")
        if (m := DB_FILENAME_RE.match(p.name))
    ]
    return max(candidates, key=lambda item: item[1], default=None)


def ensure_local_database_current(db_dir: Path, max_age: timedelta) -> Path:
    """Returns the newest local db file, downloading a fresh copy first if it
    is missing or older than `max_age`."""
    db_dir.mkdir(parents=True, exist_ok=True)

    latest = _find_latest_local_db(db_dir)
    if latest is None:
        logging.info("No local SQLite database found in %s. Fetching from remote...", db_dir)
    else:
        path, ts = latest
        age = datetime.now() - ts
        if age < max_age:
            logging.info("Local SQLite database is current (%s, age %s).", path.name, age)
            return path
        logging.info("Local SQLite database is stale (%s, age %s >= %s). Fetching from remote...", path.name, age, max_age)

    new_path = db_dir / f"reduced_data_{datetime.now():{DB_FILENAME_TS_FORMAT}}.db"
    sync_remote_database(new_path)
    return new_path


def sync_remote_database(local_path: Path) -> None:
    """Download the remote database and atomically replace `local_path` with it."""
    try:
        import paramiko
    except ImportError as error:
        raise RemoteSyncError("paramiko is not installed") from error

    ssh = SSHSettings.from_environment()
    remote_path = _env_required("RTI_SQLITE_REMOTE_PATH")

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")

    client = paramiko.SSHClient()
    client.load_host_keys(str(ssh.known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    logging.info("Connecting to %s:%s to fetch remote SQLite database...", ssh.host, ssh.port)
    try:
        client.connect(
            hostname=ssh.host,
            port=ssh.port,
            username=ssh.user,
            password=ssh.password,
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        with client.open_sftp() as sftp:
            logging.info("Downloading %s -> %s", remote_path, tmp_path)
            sftp.get(remote_path, str(tmp_path))
    except Exception as error:
        tmp_path.unlink(missing_ok=True)
        raise RemoteSyncError(f"Failed to fetch remote SQLite database: {error}") from error
    finally:
        client.close()

    os.replace(tmp_path, local_path)
    logging.info("Local database updated: %s", local_path)
