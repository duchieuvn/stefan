#!/usr/bin/env python3
"""Sync SQLite to PostgreSQL while keeping only every Nth round.

This script does not modify existing sync code. It provides a standalone path
for reduced-size destination sync:
- Rounds: keep only rows where round_id % step == 0
- Measurements: keep only rows with non-null round_id that match the same step

It can run once or on a schedule.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable

import psycopg2
from dotenv import load_dotenv

import sync_postgres
from log_config import logger


ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)

SQLITE_PATH = Path(os.getenv("SQLITE_PATH"))
DEFAULT_STEP = int(os.getenv("REDUCE_STEP", "5"))
DEFAULT_BATCH_SIZE = 1000
DEFAULT_INTERVAL_SECONDS = 2 * 60 * 60
MAX_RETRIES = 5
INITIAL_RETRY_DELAY_SECONDS = 5

DatabasePosition = tuple[int, int]


ROUND_COLUMNS = sync_postgres.ROUND_COLUMNS
MEASUREMENT_COLUMNS = sync_postgres.MEASUREMENT_COLUMNS


def load_postgres_dsn(connection_type: str = "tailscale") -> str:
    """Load DSN for local or tailscale mode with backward-compatible env keys."""
    if connection_type == "tailscale":
        dsn = os.getenv("TAILSCALE_REDUCED_DSN")
    elif connection_type == "local":
        dsn = (os.getenv("LOCAL_REDUCED_DSN"))
    else:
        raise ValueError("Invalid connection type. Use 'tailscale' or 'local'.")

    if not dsn:
        raise RuntimeError(f"{connection_type.upper()} DSN is missing in {ENV_PATH}")
    return dsn


def connect_sqlite() -> sqlite3.Connection:
    """Open source SQLite in read-only mode to avoid write locks."""
    if not SQLITE_PATH.is_file():
        raise FileNotFoundError(f"SQLite database not found: {SQLITE_PATH}")

    connection = sqlite3.connect(
        f"file:{SQLITE_PATH.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def postgres_position(connection_type: str) -> DatabasePosition:
    """Return current destination high-water marks."""
    dsn = load_postgres_dsn(connection_type)
    with sync_postgres.postgres_connect(dsn) as connection:
        sync_postgres.create_postgres_schema(connection)
        return (
            sync_postgres.postgres_max(connection, "rounds", "round_id"),
            sync_postgres.postgres_max(connection, "measurement", "id"),
        )


def sqlite_targets_reduced(
    sqlite_connection: sqlite3.Connection,
    step: int,
) -> DatabasePosition:
    """Capture reduced source targets for one scheduler run snapshot."""
    round_target = sqlite_connection.execute(
        "SELECT COALESCE(MAX(round_id), 0) FROM rounds WHERE round_id % ? = 0",
        (step,),
    ).fetchone()[0]

    measurement_target = sqlite_connection.execute(
        """
        SELECT COALESCE(MAX(id), 0)
        FROM measurement
        WHERE round_id IS NOT NULL
          AND round_id % ? = 0
          AND round_id <= ?
        """,
        (step, round_target),
    ).fetchone()[0]

    return int(round_target), int(measurement_target)


def format_position(position: DatabasePosition) -> str:
    return f"round_id={position[0]}, measurement_id={position[1]}"


def targets_reached(position: DatabasePosition, targets: DatabasePosition) -> bool:
    return position[0] >= targets[0] and position[1] >= targets[1]


def fetch_rounds_reduced(
    sqlite_connection: sqlite3.Connection,
    after_id: int,
    step: int,
    batch_size: int,
) -> list[tuple]:
    columns = ", ".join(ROUND_COLUMNS)
    rows = sqlite_connection.execute(
        f"SELECT {columns} FROM rounds "
        "WHERE round_id > ? AND round_id % ? = 0 "
        "ORDER BY round_id LIMIT ?",
        (after_id, step, batch_size),
    ).fetchall()
    return [tuple(row[column] for column in ROUND_COLUMNS) for row in rows]


def fetch_measurements_reduced(
    sqlite_connection: sqlite3.Connection,
    after_id: int,
    available_round_id: int,
    step: int,
    batch_size: int,
) -> list[tuple]:
    columns = ", ".join(MEASUREMENT_COLUMNS)
    rows = sqlite_connection.execute(
        f"SELECT {columns} FROM measurement "
        "WHERE id > ? "
        "  AND round_id IS NOT NULL "
        "  AND round_id <= ? "
        "  AND round_id % ? = 0 "
        "ORDER BY id LIMIT ?",
        (after_id, available_round_id, step, batch_size),
    ).fetchall()
    return [tuple(row[column] for column in MEASUREMENT_COLUMNS) for row in rows]


def sync_table(
    connection: psycopg2.extensions.connection,
    table: str,
    key: str,
    columns: tuple[str, ...],
    rows: list[tuple],
    batch_size: int,
) -> None:
    previous_max = sync_postgres.postgres_max(connection, table, key)
    inserted = sync_postgres.insert_rows(connection, table, columns, rows)
    new_max = sync_postgres.postgres_max(connection, table, key)
    logger.info(
        f"{table}: previous {key}={previous_max}, "
        f"selected={len(rows)}, inserted={inserted}, new {key}={new_max}"
    )
    if len(rows) == batch_size:
        logger.warning(
            f"{table}: batch limit reached; another batch may be required."
        )


def reduced_batch_sync(
    connection_type: str,
    step: int,
    batch_size: int,
) -> None:
    """Sync one reduced batch for rounds, then one reduced batch for measurement."""
    dsn = load_postgres_dsn(connection_type)
    sync_postgres.test_postgres_connection(dsn)

    with closing(connect_sqlite()) as sqlite_connection:
        with sync_postgres.postgres_connect(dsn) as postgres:
            sync_postgres.create_postgres_schema(postgres)

            latest_round = sync_postgres.postgres_max(postgres, "rounds", "round_id")
            round_rows = fetch_rounds_reduced(
                sqlite_connection,
                latest_round,
                step,
                batch_size,
            )
            sync_table(
                postgres,
                "rounds",
                "round_id",
                ROUND_COLUMNS,
                round_rows,
                batch_size,
            )

            available_round = sync_postgres.postgres_max(postgres, "rounds", "round_id")
            latest_measurement = sync_postgres.postgres_max(postgres, "measurement", "id")
            measurement_rows = fetch_measurements_reduced(
                sqlite_connection,
                latest_measurement,
                available_round,
                step,
                batch_size,
            )
            sync_table(
                postgres,
                "measurement",
                "id",
                MEASUREMENT_COLUMNS,
                measurement_rows,
                batch_size,
            )


def catch_up_to_targets(
    targets: DatabasePosition,
    connection_type: str,
    step: int,
    batch_size: int,
) -> None:
    """Run reduced batches until destination reaches reduced source targets."""
    position = postgres_position(connection_type)
    logger.info(
        f"Reduced sync progress: {format_position(position)}; "
        f"targets: {format_position(targets)}"
    )

    while not targets_reached(position, targets):
        previous_position = position
        reduced_batch_sync(connection_type, step, batch_size)
        position = postgres_position(connection_type)
        logger.info(
            f"Reduced sync progress: {format_position(position)}; "
            f"targets: {format_position(targets)}"
        )

        if position == previous_position:
            raise RuntimeError(
                "Reduced sync made no progress before reaching targets "
                f"({format_position(targets)})"
            )


def run_scheduled_sync(
    connection_type: str,
    step: int,
    batch_size: int,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Execute one reduced snapshot run with bounded retries."""
    targets: DatabasePosition | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            if targets is None:
                with closing(connect_sqlite()) as sqlite_connection:
                    targets = sqlite_targets_reduced(sqlite_connection, step)
                logger.info(
                    "Starting reduced scheduled sync; captured "
                    f"{format_position(targets)} (step={step})"
                )

            catch_up_to_targets(targets, connection_type, step, batch_size)
            logger.info(
                f"Reduced scheduled sync complete at {format_position(targets)}"
            )
            return True
        except Exception as error:
            if attempt == MAX_RETRIES:
                logger.error(
                    f"Reduced scheduled sync failed after {MAX_RETRIES} retries: {error}"
                )
                return False

            delay = INITIAL_RETRY_DELAY_SECONDS * (2**attempt)
            logger.error(
                f"Reduced sync attempt {attempt + 1} failed: {error}; "
                f"retrying in {delay} seconds"
            )
            sleep(delay)

    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync SQLite to PostgreSQL with round-based reduction",
    )
    parser.add_argument(
        "--connection",
        choices=("tailscale", "local"),
        default="tailscale",
        help="Destination PostgreSQL connection profile",
    )
    parser.add_argument(
        "--step",
        type=int,
        default=DEFAULT_STEP,
        help="Keep every Nth round (default: %(default)s)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Rows per table per batch (default: %(default)s)",
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


def validate_args(step: int, batch_size: int, interval_seconds: int) -> None:
    if step <= 0:
        raise ValueError("--step must be greater than 0")
    if batch_size <= 0:
        raise ValueError("--batch-size must be greater than 0")
    if interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")


def main() -> None:
    args = parse_args()
    validate_args(args.step, args.batch_size, args.interval_seconds)

    logger.warning(
        "Reduced sync is enabled: only round_id %% %s == 0 will be copied; "
        "measurement rows with NULL round_id are excluded.",
        args.step,
    )
    logger.info(
        "Reduced sync configuration: connection=%s, batch_size=%s, step=%s",
        args.connection,
        args.batch_size,
        args.step,
    )

    dsn = load_postgres_dsn(args.connection)
    sync_postgres.test_postgres_connection(dsn)

    if args.once:
        reduced_batch_sync(args.connection, args.step, args.batch_size)
        return

    logger.info(
        "Reduced sync scheduler started (interval=%s seconds)",
        args.interval_seconds,
    )
    try:
        while True:
            run_scheduled_sync(args.connection, args.step, args.batch_size)
            logger.info(
                "Next reduced scheduled sync will start in %s seconds",
                args.interval_seconds,
            )
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        logger.info("Reduced sync scheduler stopped")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logger.exception("Reduced sync failed: %s", error)
        raise SystemExit(1) from error
