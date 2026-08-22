"""Database providers for SQLite and PostgreSQL."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

# --- Protocol Definition ---

class DatabaseProvider(Protocol):
    """Minimal database interface required by the temporal RTI loader."""
    def connect(self) -> Any: ...
    def close(self) -> None: ...

# --- SQLite Implementation ---

class _SQLiteConnectionWrapper:
    """Wraps a sqlite3.Connection to translate placeholder styles."""
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def execute(self, query: str, params: tuple | None = None):
        """Translate psycopg2's %s placeholders to sqlite3's ? and execute."""
        translated_query = query.replace("%s", "?")
        return self._connection.execute(translated_query, params or ())

    def __getattr__(self, name):
        """Delegate all other calls to the original connection object."""
        return getattr(self._connection, name)


class _PostgresConnectionWrapper:
    """Wrap psycopg2 connections with the execute interface used by the RTI core."""

    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, query: str, params: tuple | None = None):
        cursor = self._connection.cursor()
        cursor.execute(query, params or ())
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)

class SQLiteDatabase:
    """A database provider for SQLite that mimics the Postgres provider's interface."""
    def __init__(self, path: Path):
        self.path = path
        if not self.path.is_file():
            raise FileNotFoundError(f"SQLite database not found: {self.path}")
        self._ensure_indexes()

    def connect(self) -> _SQLiteConnectionWrapper:
        """Return a new SQLite connection wrapped for placeholder compatibility."""
        con = sqlite3.connect(self.path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA busy_timeout=5000")
        return _SQLiteConnectionWrapper(con)

    def _ensure_indexes(self) -> None:
        """Create indexes for faster queries. This needs a writeable connection."""
        try:
            with sqlite3.connect(self.path, timeout=10.0) as con:
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_pc_ts ON measurement(pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_link_time ON measurement(sender_id, receiver_id, pc_ts)")
                con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_ok_time ON measurement(ok_match, pc_ts)")
                con.commit()
        except sqlite3.OperationalError as e:
            # This can happen if the file is on a read-only filesystem.
            # The pre-computation will be slower but might still work.
            print(f"Warning: Could not create SQLite indexes: {e}")

    def close(self) -> None:
        """The provider object itself has no persistent connection to close."""

    @classmethod
    def from_config(cls, config) -> SQLiteDatabase:
        return cls(config.db_path)

# --- PostgreSQL Implementation ---

class DatabaseConnectionError(RuntimeError):
    """Raised when the SSH tunnel or PostgreSQL connection cannot be opened."""

class PostgresDirectDatabase:
    """A database provider for a direct PostgreSQL connection without an SSH tunnel."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self) -> Any:
        """Return a new direct PostgreSQL connection."""
        try:
            import psycopg
            return psycopg.connect(self.dsn)
        except ImportError as error:
            raise DatabaseConnectionError("psycopg is not installed") from error
        except Exception as error:
            raise DatabaseConnectionError(f"Could not connect directly to PostgreSQL: {error}") from error

    def close(self) -> None:
        """The provider object itself has no persistent connection to close."""
        pass

    @classmethod
    def from_environment(cls, env_path: Path | None = None) -> "PostgresDirectDatabase":
        if env_path is None:
            env_path = Path(".env")
        load_dotenv(env_path, override=False)

        dsn = os.getenv("RTI_POSTGRES_DSN", "").strip()
        if not dsn:
            raise DatabaseConnectionError(f"Missing required setting in {env_path}: RTI_POSTGRES_DSN")
        return cls(dsn)

@dataclass(frozen=True)
class PostgresTunnelSettings:
    postgres_dsn: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_password: str
    ssh_known_hosts: Path
    db_host: str
    db_port: int
    db_connect_timeout_s: float

    @classmethod
    def from_environment(cls, env_path: Path | None = None) -> "PostgresTunnelSettings":
        if env_path is None:
            # Assume .env is in SOFTWARE directory, where precompute_sequences.py is.
            env_path = Path(".env")
        load_dotenv(env_path, override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise DatabaseConnectionError(f"Missing required setting: {name}")
            return value

        def required_secret(name: str) -> str:
            value = os.getenv(name, "")
            if not value:
                raise DatabaseConnectionError(f"Missing required setting: {name}")
            return value

        def integer(name: str, default: int | None = None) -> int:
            raw = os.getenv(name, str(default) if default is not None else "").strip()
            try:
                value = int(raw)
            except ValueError as error:
                raise DatabaseConnectionError(f"{name} must be an integer") from error
            if not 1 <= value <= 65535:
                raise DatabaseConnectionError(f"{name} must be between 1 and 65535")
            return value

        known_hosts = Path(required("RTI_SSH_KNOWN_HOSTS")).expanduser()
        if not known_hosts.is_file():
            raise DatabaseConnectionError(
                f"RTI_SSH_KNOWN_HOSTS does not exist or is not a file: {known_hosts}"
            )

        return cls(
            postgres_dsn=required("RTI_POSTGRES_DSN"),
            ssh_host=required("RTI_SSH_HOST"),
            ssh_port=integer("RTI_SSH_PORT", 22),
            ssh_user=required("RTI_SSH_USER"),
            ssh_password=required_secret("RTI_SSH_PASSWORD"),
            ssh_known_hosts=known_hosts,
            db_host=required("RTI_DB_HOST"),
            db_port=integer("RTI_DB_PORT", 5432),
            db_connect_timeout_s=float(integer("RTI_DB_CONNECT_TIMEOUT_S", 60)),
        )

class PostgresTunnelDatabase:
    """Own a persistent SSH forwarder while returning short-lived DB sessions."""

    def __init__(self, settings: PostgresTunnelSettings) -> None:
        self.settings = settings
        self._tunnel: Any | None = None

    @classmethod
    def from_environment(cls) -> "PostgresTunnelDatabase":
        return cls(PostgresTunnelSettings.from_environment())

    def _known_host_key(self) -> Any:
        try:
            import paramiko
        except ImportError as error:
            raise DatabaseConnectionError("paramiko is not installed") from error

        host_keys = paramiko.HostKeys(filename=str(self.settings.ssh_known_hosts))
        entry = host_keys.lookup(self.settings.ssh_host)
        if entry is None:
            entry = host_keys.lookup(f"[{self.settings.ssh_host}]:{self.settings.ssh_port}")
        if not entry:
            raise DatabaseConnectionError(
                f"No trusted host key for {self.settings.ssh_host} in {self.settings.ssh_known_hosts}"
            )
        return next(iter(entry.values()))

    def _ensure_tunnel(self) -> Any:
        if self._tunnel is not None and self._tunnel.is_active:
            return self._tunnel
        self.close()
        try:
            from sshtunnel import SSHTunnelForwarder

            tunnel = SSHTunnelForwarder(
                (self.settings.ssh_host, self.settings.ssh_port),
                ssh_username=self.settings.ssh_user,
                ssh_password=self.settings.ssh_password,
                ssh_host_key=self._known_host_key(),
                remote_bind_address=(self.settings.db_host, self.settings.db_port),
                local_bind_address=("127.0.0.1", 0),
                set_keepalive=30.0,
            )
            tunnel.start()
            self._tunnel = tunnel
            return tunnel
        except DatabaseConnectionError:
            raise
        except Exception as error:
            self.close()
            raise DatabaseConnectionError(f"Could not open SSH tunnel: {error}") from error

    def connect(self) -> Any:
        """Return a new PostgreSQL connection through the active local forward."""
        tunnel = self._ensure_tunnel()
        try:
            import psycopg2

            connection = psycopg2.connect(
                self.settings.postgres_dsn,
                host="127.0.0.1",
                port=tunnel.local_bind_port,
            )
            return _PostgresConnectionWrapper(connection)
        except Exception as error:
            self.invalidate()
            raise DatabaseConnectionError(f"Could not connect to PostgreSQL: {error}") from error

    def invalidate(self) -> None:
        """Discard a failed forwarder; the next connection recreates it."""
        self.close()

    def close(self) -> None:
        if self._tunnel is None:
            return
        tunnel, self._tunnel = self._tunnel, None
        try:
            tunnel.stop()
        except Exception:
            pass


@dataclass(frozen=True)
class PostgresSettings:
    postgres_dsn: str

    @classmethod
    def from_environment(cls, env_path: Path | None = None) -> "PostgresSettings":
        if env_path is None:
            env_path = Path(".env")
        load_dotenv(env_path, override=False)

        postgres_dsn = os.getenv("RTI_TAILSCALE_POSTGRES_DSN", "").strip()
        if not postgres_dsn:
            raise DatabaseConnectionError("Missing required setting: RTI_TAILSCALE_POSTGRES_DSN")
        return cls(postgres_dsn=postgres_dsn)


class PostgresDatabase:
    """Connect directly to PostgreSQL using its Tailscale address."""

    def __init__(self, settings: PostgresSettings) -> None:
        self.settings = settings

    @classmethod
    def from_environment(cls) -> "PostgresDatabase":
        return cls(PostgresSettings.from_environment())

    def connect(self) -> Any:
        try:
            import psycopg2

            return _PostgresConnectionWrapper(psycopg2.connect(self.settings.postgres_dsn))
        except Exception as error:
            raise DatabaseConnectionError(
                f"Could not connect directly to PostgreSQL: {error}"
            ) from error

    def close(self) -> None:
        """The provider has no persistent connection to close."""