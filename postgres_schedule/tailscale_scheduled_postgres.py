#!/usr/bin/env python3
"""Run the SQLite-to-PostgreSQL synchronization on a two-hour schedule."""

from contextlib import closing
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import time
from typing import Callable

import psycopg
import sync_postgres
from dotenv import load_dotenv


RUN_INTERVAL_SECONDS = 2 * 60 * 60
MAX_RETRIES = 5
INITIAL_RETRY_DELAY_SECONDS = 5
ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)
LOG_DIRECTORY = Path(
    os.getenv("LOG_DIRECTORY", "logs/tailscale_scheduled_postgres/")
).expanduser()
INFO_LOG_PATH = LOG_DIRECTORY / "info.log"
ERROR_LOG_PATH = LOG_DIRECTORY / "error.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

DatabasePosition = tuple[int, int]


def configure_logging() -> logging.Logger:
    """Configure separate rotating files for informational and error logs."""
    logger = logging.getLogger("tailscale_scheduled_postgres")
    if logger.handlers:
        return logger

    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    info_handler = RotatingFileHandler(
        INFO_LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(
        ERROR_LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(console_handler)
    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.propagate = False
    return logger


logger = configure_logging()


def sqlite_targets() -> DatabasePosition:
    """Capture the source high-water marks for one scheduled run."""
    with closing(sync_postgres.connect_sqlite()) as connection:
        round_id = connection.execute(
            "SELECT COALESCE(MAX(round_id), 0) FROM rounds"
        ).fetchone()[0]
        measurement_id = connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM measurement"
        ).fetchone()[0]
    return int(round_id), int(measurement_id)


def postgres_position() -> DatabasePosition:
    """Return the destination high-water marks, creating its schema if needed."""
    dsn = sync_postgres.load_postgres_dsn("tailscale")
    with psycopg.connect(dsn) as connection:
        sync_postgres.create_postgres_schema(connection)
        return (
            sync_postgres.postgres_max(connection, "rounds", "round_id"),
            sync_postgres.postgres_max(connection, "measurement", "id"),
        )


def format_position(position: DatabasePosition) -> str:
    return f"round_id={position[0]}, measurement_id={position[1]}"


def targets_reached(
    position: DatabasePosition,
    targets: DatabasePosition,
) -> bool:
    return position[0] >= targets[0] and position[1] >= targets[1]


def catch_up_to(targets: DatabasePosition) -> None:
    """Run 1,000-row sync batches until both captured targets are reached."""
    position = postgres_position()
    logger.info(
        f"Sync progress: {format_position(position)}; "
        f"targets: {format_position(targets)}"
    )

    while not targets_reached(position, targets):
        previous_position = position
        try:
            sync_postgres.batch_sync("tailscale")
        except ConnectionError as error:
            logger.error("PostgreSQL connection error: %s", error)
            raise
        position = postgres_position()
        logger.info(
            f"Sync progress: {format_position(position)}; "
            f"targets: {format_position(targets)}"
        )

        if position == previous_position:
            raise RuntimeError(
                "Sync made no progress before reaching its captured targets "
                f"({format_position(targets)})"
            )


def run_scheduled_sync(
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Execute one snapshot-based run with bounded exponential-backoff retries."""
    targets: DatabasePosition | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            if targets is None:
                targets = sqlite_targets()
                logger.info(
                    "Starting scheduled sync; captured "
                    f"{format_position(targets)}"
                )
            catch_up_to(targets)
            logger.info(f"Scheduled sync complete at {format_position(targets)}")
            return True
        except Exception as error:
            if attempt == MAX_RETRIES:
                logger.error(
                    f"Scheduled sync failed after {MAX_RETRIES} retries: {error}",
                )
                return False

            delay = INITIAL_RETRY_DELAY_SECONDS * (2**attempt)
            logger.error(
                f"Sync attempt {attempt + 1} failed: {error}; "
                f"retrying in {delay} seconds",
            )
            sleep(delay)

    return False


def main() -> None:
    """Run immediately, then wait two hours after each run completes."""
    logger.info(
        "PostgreSQL sync scheduler started "
        f"(batch size={sync_postgres.BATCH_SIZE}, interval=2 hours)"
    )
    try:
        while True:
            run_scheduled_sync()
            logger.info("Next scheduled sync will start in 2 hours")
            time.sleep(RUN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("PostgreSQL sync scheduler stopped")


if __name__ == "__main__":
    main()
