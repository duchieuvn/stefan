#!/usr/bin/env python3
"""Sync a source SQLite database to a target PostgreSQL database, once or on a schedule.

Source/target pairs are named profiles configured in .env next to this file:

    <SOURCE>_SQLITE_PATH          e.g. MAIN_SQLITE_PATH
    <SOURCE>_<CONNECTION>_DSN     e.g. MAIN_TAILSCALE_DSN, REDUCED_LOCAL_DSN

Rows are copied incrementally by key (rounds.round_id, measurement.id) in
batches of BATCH_SIZE; existing rows are skipped (ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import psycopg2  # currently only works with psycopg2 2.9.10
from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH)

SOURCES = ("main", "reduced")
CONNECTIONS = ("tailscale", "local")

BATCH_SIZE = 1000
DEFAULT_INTERVAL_SECONDS = 2 * 60 * 60
MAX_RETRIES = 5
INITIAL_RETRY_DELAY_SECONDS = 5

POSTGRES_CONNECT_TIMEOUT_SECONDS = 30
POSTGRES_STATEMENT_TIMEOUT_MS = 120_000
POSTGRES_LOCK_TIMEOUT_MS = 30_000

# Column name -> PostgreSQL type. The column order is also the copy order.
ROUNDS_SCHEMA = {
    "round_id": "BIGINT PRIMARY KEY",
    "pc_start": "TEXT",
    "pc_end": "TEXT",
    "duration_s": "DOUBLE PRECISION",
    "nodes_csv": "TEXT",
    "payload_len": "BIGINT",
    "payload_text": "TEXT",
    "ok_links": "BIGINT",
    "total_links": "BIGINT",
    "timeouts": "BIGINT",
    "freq_mhz": "DOUBLE PRECISION",
    "tx_power": "BIGINT",
    "sf": "BIGINT",
    "bw_khz": "BIGINT",
    "cr_denom": "BIGINT",
    "lora_applied": "BIGINT",
    "port": "TEXT",
    "baudrate": "BIGINT",
    "measure_interval_s": "DOUBLE PRECISION",
    "notes": "TEXT",
}
MEASUREMENT_SCHEMA = {
    "id": "BIGINT PRIMARY KEY",
    "pc_ts": "TEXT NOT NULL",
    "round_id": "BIGINT REFERENCES rounds(round_id)",
    "sender_id": "BIGINT",
    "receiver_id": "BIGINT",
    "marker": "BIGINT",
    "payload_len": "BIGINT",
    "payload_text": "TEXT",
    "trigger_ms": "DOUBLE PRECISION",
    "rx_wait_ms": "DOUBLE PRECISION",
    "e2e_ms": "DOUBLE PRECISION",
    "ok_match": "BIGINT",
    "rx_id": "BIGINT",
    "node_timestamp": "BIGINT",
    "rssi": "DOUBLE PRECISION",
    "snr": "DOUBLE PRECISION",
    "bme_wait_ms": "DOUBLE PRECISION",
    "bme_temp": "DOUBLE PRECISION",
    "bme_hum": "DOUBLE PRECISION",
    "bme_press": "DOUBLE PRECISION",
}

# (max round_id, max measurement id); the same query works on SQLite and PostgreSQL.
Position = tuple[int, int]
POSITION_SQL = (
    "SELECT (SELECT COALESCE(MAX(round_id), 0) FROM rounds), "
    "(SELECT COALESCE(MAX(id), 0) FROM measurement)"
)


def log(message: str) -> None:
    # flush so output appears immediately when redirected to a file (nohup)
    print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}", flush=True)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set in {ENV_PATH}")
    return value


def format_position(position: Position) -> str:
    return f"round_id={position[0]}, measurement_id={position[1]}"


# --- Connections ---

def connect_sqlite(path: Path) -> sqlite3.Connection:
    """Open the source read-only so its writer is never blocked."""
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def connect_postgres(dsn: str) -> psycopg2.extensions.connection:
    """Open PostgreSQL with bounded connection, query, and lock waits."""
    return psycopg2.connect(
        dsn,
        connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
        options=(
            f"-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={POSTGRES_LOCK_TIMEOUT_MS}"
        ),
    )


def create_postgres_schema(pg: psycopg2.extensions.connection) -> None:
    """Create the destination tables when running against an empty database."""
    with pg, pg.cursor() as cursor:
        for table, schema in (("rounds", ROUNDS_SCHEMA), ("measurement", MEASUREMENT_SCHEMA)):
            columns = ", ".join(f"{name} {type_}" for name, type_ in schema.items())
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns})")
        cursor.execute("CREATE INDEX IF NOT EXISTS measurement_round_id_idx ON measurement(round_id)")


# --- Sync ---

def sqlite_position(src: sqlite3.Connection) -> Position:
    round_id, measurement_id = src.execute(POSITION_SQL).fetchone()
    return int(round_id), int(measurement_id)


def postgres_position(pg: psycopg2.extensions.connection) -> Position:
    with pg.cursor() as cursor:
        cursor.execute(POSITION_SQL)
        round_id, measurement_id = cursor.fetchone()
    return int(round_id), int(measurement_id)


def copy_batch(
    src: sqlite3.Connection,
    pg: psycopg2.extensions.connection,
    table: str,
    columns: tuple[str, ...],
    where: str,
    params: tuple,
) -> None:
    """Copy up to BATCH_SIZE rows of `table` matching `where` (incl. ORDER BY)."""
    column_list = ", ".join(columns)
    rows = src.execute(
        f"SELECT {column_list} FROM {table} WHERE {where} LIMIT ?", (*params, BATCH_SIZE)
    ).fetchall()

    inserted = 0
    if rows:
        placeholders = ", ".join(["%s"] * len(columns))
        with pg, pg.cursor() as cursor:  # one transaction: commit on success, rollback on error
            cursor.executemany(
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                rows,
            )
            inserted = cursor.rowcount

    more = " (batch limit reached, more rows pending)" if len(rows) == BATCH_SIZE else ""
    log(f"{table}: selected={len(rows)}, inserted={inserted}{more}")


def sync_batch(src: sqlite3.Connection, pg: psycopg2.extensions.connection) -> Position:
    """Copy the next batch of rounds, then of measurements whose round already
    exists in PostgreSQL. Returns the new PostgreSQL position."""
    round_id, measurement_id = postgres_position(pg)
    copy_batch(src, pg, "rounds", tuple(ROUNDS_SCHEMA), "round_id > ? ORDER BY round_id", (round_id,))

    round_id, _ = postgres_position(pg)
    copy_batch(
        src, pg, "measurement", tuple(MEASUREMENT_SCHEMA),
        "id > ? AND (round_id IS NULL OR round_id <= ?) ORDER BY id",
        (measurement_id, round_id),
    )
    return postgres_position(pg)


def catch_up(sqlite_path: Path, dsn: str, target: Position) -> None:
    """Run batches until PostgreSQL reaches `target` (a snapshot of the source)."""
    with closing(connect_sqlite(sqlite_path)) as src, closing(connect_postgres(dsn)) as pg:
        create_postgres_schema(pg)
        position = postgres_position(pg)
        log(f"Progress: {format_position(position)}; target: {format_position(target)}")

        while position[0] < target[0] or position[1] < target[1]:
            previous = position
            position = sync_batch(src, pg)
            log(f"Progress: {format_position(position)}; target: {format_position(target)}")
            if position == previous:
                raise RuntimeError(f"Sync made no progress before reaching {format_position(target)}")


def run_scheduled_sync(sqlite_path: Path, dsn: str) -> bool:
    """One scheduled run: snapshot the source, then catch up with exponential-backoff retries."""
    target: Position | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            if target is None:
                with closing(connect_sqlite(sqlite_path)) as src:
                    target = sqlite_position(src)
                log(f"Starting scheduled sync; target {format_position(target)}")
            catch_up(sqlite_path, dsn, target)
            log(f"Scheduled sync complete at {format_position(target)}")
            return True
        except Exception as error:
            if attempt == MAX_RETRIES:
                log(f"ERROR: Scheduled sync failed after {MAX_RETRIES} retries: {error}")
                return False
            delay = INITIAL_RETRY_DELAY_SECONDS * 2**attempt
            log(f"ERROR: Sync attempt {attempt + 1} failed: {error}; retrying in {delay} seconds")
            time.sleep(delay)
    return False


# --- CLI ---

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync a source SQLite database to a target PostgreSQL database")
    parser.add_argument("--source", choices=SOURCES, default="main",
                        help="Source/target profile, configured in .env (default: %(default)s)")
    parser.add_argument("--connection", choices=CONNECTIONS, default="tailscale",
                        help="Destination PostgreSQL connection profile (default: %(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Copy one batch and exit")
    mode.add_argument("--check", action="store_true", help="Only test the PostgreSQL connection")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS,
                        help="Interval between scheduled runs (default: %(default)s)")
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than 0")

    sqlite_path = Path(require_env(f"{args.source.upper()}_SQLITE_PATH")).expanduser()
    dsn = require_env(f"{args.source.upper()}_{args.connection.upper()}_DSN")
    log(f"Sync configuration: source={args.source}, connection={args.connection}, sqlite_path={sqlite_path}")

    connect_postgres(dsn).close()
    log("PostgreSQL connection successful")
    if args.check:
        return

    if args.once:
        with closing(connect_sqlite(sqlite_path)) as src, closing(connect_postgres(dsn)) as pg:
            create_postgres_schema(pg)
            log(f"Synced to {format_position(sync_batch(src, pg))}")
        return

    log(f"Sync scheduler started (interval={args.interval_seconds} seconds)")
    try:
        while True:
            run_scheduled_sync(sqlite_path, dsn)
            log(f"Next scheduled sync will start in {args.interval_seconds} seconds")
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        log("Sync scheduler stopped")


if __name__ == "__main__":
    main()
