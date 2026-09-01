#!/usr/bin/env python3
"""Sync a source SQLite database to a target PostgreSQL database on a schedule.

Source/target pairs are named profiles defined in sync_config.py (e.g.
"main", "reduced"); --connection picks the tailscale or local DSN within
that profile. Replaces scheduled_sync_postgres.py, tailscale_scheduled_postgres.py,
and sync_reduced_postgres.py, which each hardcoded one source/target pair.

It can run once or on a schedule.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from typing import Callable

import sync_config
import sync_postgres
from log_config import logger


DEFAULT_INTERVAL_SECONDS = 2 * 60 * 60
MAX_RETRIES = 5
INITIAL_RETRY_DELAY_SECONDS = 5

DatabasePosition = tuple[int, int]


def connect_sqlite(path: Path) -> sqlite3.Connection:
    """Open source SQLite in read-only mode to avoid write locks."""
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")

    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def sqlite_targets(sqlite_connection: sqlite3.Connection) -> DatabasePosition:
    """Capture the source high-water marks for one scheduled run."""
    round_id = sqlite_connection.execute(
        "SELECT COALESCE(MAX(round_id), 0) FROM rounds"
    ).fetchone()[0]
    measurement_id = sqlite_connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM measurement"
    ).fetchone()[0]
    return int(round_id), int(measurement_id)


def postgres_position(dsn: str) -> DatabasePosition:
    """Return the destination high-water marks, creating its schema if needed."""
    with sync_postgres.postgres_connect(dsn) as connection:
        sync_postgres.create_postgres_schema(connection)
        return (
            sync_postgres.postgres_max(connection, "rounds", "round_id"),
            sync_postgres.postgres_max(connection, "measurement", "id"),
        )


def format_position(position: DatabasePosition) -> str:
    return f"round_id={position[0]}, measurement_id={position[1]}"


def targets_reached(position: DatabasePosition, targets: DatabasePosition) -> bool:
    return position[0] >= targets[0] and position[1] >= targets[1]


def batch_sync(sqlite_path: Path, dsn: str) -> None:
    """Copy the next batch of rounds and measurements from source to target."""
    sync_postgres.test_postgres_connection(dsn)

    with closing(connect_sqlite(sqlite_path)) as sqlite_connection:
        with sync_postgres.postgres_connect(dsn) as postgres:
            sync_postgres.create_postgres_schema(postgres)

            latest_round = sync_postgres.postgres_max(postgres, "rounds", "round_id")
            round_rows = sync_postgres.fetch_rounds(sqlite_connection, latest_round)
            sync_postgres.sync_table(
                postgres, "rounds", "round_id", sync_postgres.ROUND_COLUMNS, round_rows
            )

            available_round = sync_postgres.postgres_max(postgres, "rounds", "round_id")
            latest_measurement = sync_postgres.postgres_max(postgres, "measurement", "id")
            measurement_rows = sync_postgres.fetch_measurements(
                sqlite_connection, latest_measurement, available_round
            )
            sync_postgres.sync_table(
                postgres,
                "measurement",
                "id",
                sync_postgres.MEASUREMENT_COLUMNS,
                measurement_rows,
            )


def catch_up_to(targets: DatabasePosition, sqlite_path: Path, dsn: str) -> None:
    """Run sync batches until the destination reaches the captured targets."""
    position = postgres_position(dsn)
    logger.info(
        f"Sync progress: {format_position(position)}; "
        f"targets: {format_position(targets)}"
    )

    while not targets_reached(position, targets):
        previous_position = position
        batch_sync(sqlite_path, dsn)
        position = postgres_position(dsn)
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
    sqlite_path: Path,
    dsn: str,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Execute one snapshot-based run with bounded exponential-backoff retries."""
    targets: DatabasePosition | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            if targets is None:
                with closing(connect_sqlite(sqlite_path)) as sqlite_connection:
                    targets = sqlite_targets(sqlite_connection)
                logger.info(
                    f"Starting scheduled sync; captured {format_position(targets)}"
                )

            catch_up_to(targets, sqlite_path, dsn)
            logger.info(f"Scheduled sync complete at {format_position(targets)}")
            return True
        except Exception as error:
            if attempt == MAX_RETRIES:
                logger.error(
                    f"Scheduled sync failed after {MAX_RETRIES} retries: {error}"
                )
                return False

            delay = INITIAL_RETRY_DELAY_SECONDS * (2**attempt)
            logger.error(
                f"Sync attempt {attempt + 1} failed: {error}; "
                f"retrying in {delay} seconds"
            )
            sleep(delay)

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync a source SQLite database to a target PostgreSQL database",
    )
    parser.add_argument(
        "--source",
        choices=tuple(sync_config.SQLITE_PATHS),
        default="main",
        help="Source/target database profile, defined in sync_config.py (default: %(default)s)",
    )
    parser.add_argument(
        "--connection",
        choices=("tailscale", "local"),
        default="tailscale",
        help="Destination PostgreSQL connection profile (default: %(default)s)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Interval between scheduled runs (default: %(default)s)",
    )
    return parser.parse_args()


def validate_args(interval_seconds: int) -> None:
    if interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")


def main() -> None:
    args = parse_args()
    validate_args(args.interval_seconds)

    sqlite_path = sync_config.sqlite_path(args.source)
    dsn = sync_config.postgres_dsn(args.source, args.connection)

    logger.info(
        "Sync configuration: source=%s, connection=%s, sqlite_path=%s",
        args.source,
        args.connection,
        sqlite_path,
    )
    sync_postgres.test_postgres_connection(dsn)

    if args.once:
        batch_sync(sqlite_path, dsn)
        return

    logger.info("Sync scheduler started (interval=%s seconds)", args.interval_seconds)
    try:
        while True:
            run_scheduled_sync(sqlite_path, dsn)
            logger.info(
                "Next scheduled sync will start in %s seconds", args.interval_seconds
            )
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        logger.info("Sync scheduler stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("Sync failed: %s", error)
        raise SystemExit(1) from error


# # Run once, main profile, over tailscale (defaults)
# python sync_scheduled_postgres.py --once

# # Run once, main profile, connecting to local postgres instead
# python sync_scheduled_postgres.py --once --connection local

# # Run once, reduced profile (fill in sync_config.py's reduced entries first)
# python sync_scheduled_postgres.py --once --source reduced

# # Run continuously on the default schedule (every 2 hours)
# python sync_scheduled_postgres.py

# # Run continuously with a custom interval (seconds) 1800 = 30 minutes
# python sync_scheduled_postgres.py --interval-seconds 1800

# # Background it so it survives after the shell exits
# nohup python sync_scheduled_postgres.py --source reduced > reduced_schedule.log 2>&1 &