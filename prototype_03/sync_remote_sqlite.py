"""Fetches the reduced SQLite database from the remote Tailscale-connected host.

The remote machine (a Raspberry Pi running the sensor collector) has no
network-accessible SQLite server, so the database file itself is pulled over
SFTP and used to fully replace the local copy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class RemoteSyncError(RuntimeError):
    """Raised when the remote SQLite database cannot be fetched."""


@dataclass(frozen=True)
class RemoteSQLiteSettings:
    host: str
    port: int
    user: str
    password: str
    known_hosts: Path
    remote_path: str

    @classmethod
    def from_environment(cls, env_path: Path | None = None) -> "RemoteSQLiteSettings":
        if env_path is None:
            env_path = Path(".env")
        load_dotenv(env_path, override=False)

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise RemoteSyncError(f"Missing required setting in {env_path}: {name}")
            return value

        def required_secret(name: str) -> str:
            value = os.getenv(name, "")
            if not value:
                raise RemoteSyncError(f"Missing required setting in {env_path}: {name}")
            return value

        known_hosts = Path(required("RTI_SQLITE_SSH_KNOWN_HOSTS")).expanduser()
        if not known_hosts.is_file():
            raise RemoteSyncError(
                f"RTI_SQLITE_SSH_KNOWN_HOSTS does not exist or is not a file: {known_hosts}"
            )

        port_raw = os.getenv("RTI_SQLITE_SSH_PORT", "22").strip()
        try:
            port = int(port_raw)
        except ValueError as error:
            raise RemoteSyncError("RTI_SQLITE_SSH_PORT must be an integer") from error

        return cls(
            host=required("RTI_SQLITE_SSH_HOST"),
            port=port,
            user=required("RTI_SQLITE_SSH_USER"),
            password=required_secret("RTI_SQLITE_SSH_PASSWORD"),
            known_hosts=known_hosts,
            remote_path=required("RTI_SQLITE_REMOTE_PATH"),
        )


def sync_remote_database(local_path: Path, settings: RemoteSQLiteSettings | None = None) -> None:
    """Download the remote database and atomically replace `local_path` with it."""
    try:
        import paramiko
    except ImportError as error:
        raise RemoteSyncError("paramiko is not installed") from error

    if settings is None:
        settings = RemoteSQLiteSettings.from_environment()

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")

    client = paramiko.SSHClient()
    client.load_host_keys(str(settings.known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    logging.info("Connecting to %s:%s to fetch remote SQLite database...", settings.host, settings.port)
    try:
        client.connect(
            hostname=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.password,
            timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        with client.open_sftp() as sftp:
            logging.info("Downloading %s -> %s", settings.remote_path, tmp_path)
            sftp.get(settings.remote_path, str(tmp_path))
    except Exception as error:
        tmp_path.unlink(missing_ok=True)
        raise RemoteSyncError(f"Failed to fetch remote SQLite database: {error}") from error
    finally:
        client.close()

    os.replace(tmp_path, local_path)
    logging.info("Local database updated: %s", local_path)
