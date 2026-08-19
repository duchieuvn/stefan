#!/usr/bin/env python3
"""Copy the next batch of sensor data from SQLite to PostgreSQL."""

from contextlib import closing
import os
from pathlib import Path
import sqlite3

import psycopg
from dotenv import load_dotenv


ENV_PATH = Path(".env")
load_dotenv(ENV_PATH)

SQLITE_PATH = Path(os.getenv("SQLITE_PATH"))
BATCH_SIZE = 1000
POSTGRES_CONNECT_TIMEOUT_SECONDS = 30
POSTGRES_STATEMENT_TIMEOUT_MS = 120_000
POSTGRES_LOCK_TIMEOUT_MS = 30_000

ROUND_COLUMNS = (
    "round_id", "pc_start", "pc_end", "duration_s", "nodes_csv",
    "payload_len", "payload_text", "ok_links", "total_links", "timeouts",
    "freq_mhz", "tx_power", "sf", "bw_khz", "cr_denom", "lora_applied",
    "port", "baudrate", "measure_interval_s", "notes",
)

MEASUREMENT_COLUMNS = (
    "id", "pc_ts", "round_id", "sender_id", "receiver_id", "marker",
    "payload_len", "payload_text", "trigger_ms", "rx_wait_ms", "e2e_ms",
    "ok_match", "rx_id", "node_timestamp", "rssi", "snr", "bme_wait_ms",
    "bme_temp", "bme_hum", "bme_press",
)


def load_postgres_dsn(connection_type: str = "local") -> str:
    """
    Load the PostgreSQL connection string based on the connection type.

    Argument:
        connection_type: "tailscale" or "local"
    Returns:
        The PostgreSQL connection string.
    """

    if connection_type == "tailscale":
        dsn = os.getenv("TAILSCALE_POSTGRES_DSN", "postgresql://holzofen:biofilter@100.100.51.120:5454/sensor_data")
    elif connection_type == "local":
        dsn = os.getenv("LOCAL_POSTGRES_DSN", "postgresql://holzofen:biofilter@localhost:5432/sensor_data")
    else:
        raise ValueError("Invalid connection type. Use 'tailscale' or 'local'.")
    if not dsn:
        raise RuntimeError(f"{connection_type.upper()}_POSTGRES_DSN is missing in {ENV_PATH}")
    return dsn


def connect_sqlite() -> sqlite3.Connection:
    """Open the source database in read-only mode without blocking its writer."""
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


def create_postgres_schema(connection: psycopg.Connection) -> None:
    """Create destination tables when running against an empty database."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                round_id BIGINT PRIMARY KEY,
                pc_start TEXT,
                pc_end TEXT,
                duration_s DOUBLE PRECISION,
                nodes_csv TEXT,
                payload_len BIGINT,
                payload_text TEXT,
                ok_links BIGINT,
                total_links BIGINT,
                timeouts BIGINT,
                freq_mhz DOUBLE PRECISION,
                tx_power BIGINT,
                sf BIGINT,
                bw_khz BIGINT,
                cr_denom BIGINT,
                lora_applied BIGINT,
                port TEXT,
                baudrate BIGINT,
                measure_interval_s DOUBLE PRECISION,
                notes TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS measurement (
                id BIGINT PRIMARY KEY,
                pc_ts TEXT NOT NULL,
                round_id BIGINT REFERENCES rounds(round_id),
                sender_id BIGINT,
                receiver_id BIGINT,
                marker BIGINT,
                payload_len BIGINT,
                payload_text TEXT,
                trigger_ms DOUBLE PRECISION,
                rx_wait_ms DOUBLE PRECISION,
                e2e_ms DOUBLE PRECISION,
                ok_match BIGINT,
                rx_id BIGINT,
                node_timestamp BIGINT,
                rssi DOUBLE PRECISION,
                snr DOUBLE PRECISION,
                bme_wait_ms DOUBLE PRECISION,
                bme_temp DOUBLE PRECISION,
                bme_hum DOUBLE PRECISION,
                bme_press DOUBLE PRECISION
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS measurement_round_id_idx "
            "ON measurement(round_id)"
        )
    connection.commit()


def postgres_max(connection: psycopg.Connection, table: str, key: str) -> int:
    """Return the highest synchronized key, or zero for an empty table."""
    allowed = {("rounds", "round_id"), ("measurement", "id")}
    if (table, key) not in allowed:
        raise ValueError("Unsupported table/key combination")

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT COALESCE(MAX({key}), 0) FROM {table}")
        return int(cursor.fetchone()[0])


def test_postgres_connection(dsn: str) -> None:
    """Verify PostgreSQL is reachable before starting a sync."""
    try:
        with postgres_connect(dsn) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error as error:
        raise ConnectionError(f"PostgreSQL connection failed: {error}") from error


def postgres_connect(dsn: str) -> psycopg.Connection:
    """Open PostgreSQL with bounded connection, query, and lock waits."""
    return psycopg.connect(
        dsn,
        connect_timeout=POSTGRES_CONNECT_TIMEOUT_SECONDS,
        options=(
            f"-c statement_timeout={POSTGRES_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={POSTGRES_LOCK_TIMEOUT_MS}"
        ),
    )


def fetch_rounds(sqlite_connection: sqlite3.Connection, after_id: int) -> list[tuple]:
    columns = ", ".join(ROUND_COLUMNS)
    rows = sqlite_connection.execute(
        f"SELECT {columns} FROM rounds "
        "WHERE round_id > ? ORDER BY round_id LIMIT ?",
        (after_id, BATCH_SIZE),
    ).fetchall()
    return [tuple(row[column] for column in ROUND_COLUMNS) for row in rows]


def fetch_measurements(
    sqlite_connection: sqlite3.Connection,
    after_id: int,
    available_round_id: int,
) -> list[tuple]:
    columns = ", ".join(MEASUREMENT_COLUMNS)
    rows = sqlite_connection.execute(
        f"SELECT {columns} FROM measurement "
        "WHERE id > ? AND (round_id IS NULL OR round_id <= ?) "
        "ORDER BY id LIMIT ?",
        (after_id, available_round_id, BATCH_SIZE),
    ).fetchall()
    return [tuple(row[column] for column in MEASUREMENT_COLUMNS) for row in rows]


def insert_rows(
    connection: psycopg.Connection,
    table: str,
    columns: tuple[str, ...],
    rows: list[tuple],
) -> int:
    """Insert one conflict-safe batch and return the affected row count."""
    if not rows:
        return 0

    placeholders = ", ".join(["%s"] * len(columns))
    column_names = ", ".join(columns)
    statement = (
        f"INSERT INTO {table} ({column_names}) VALUES ({placeholders}) "
        "ON CONFLICT DO NOTHING"
    )

    try:
        with connection.cursor() as cursor:
            cursor.executemany(statement, rows)
            inserted = cursor.rowcount
        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise


def sync_table(
    connection: psycopg.Connection,
    table: str,
    key: str,
    columns: tuple[str, ...],
    rows: list[tuple],
) -> None:
    previous_max = postgres_max(connection, table, key)
    inserted = insert_rows(connection, table, columns, rows)
    new_max = postgres_max(connection, table, key)
    print(
        f"{table}: previous {key}={previous_max}, "
        f"selected={len(rows)}, inserted={inserted}, new {key}={new_max}"
    )
    if len(rows) == BATCH_SIZE:
        print(f"{table}: batch limit reached; run the script again for more rows.")

def batch_sync(connection_type: str) -> None:
    dsn = load_postgres_dsn(connection_type)
    test_postgres_connection(dsn)

    with closing(connect_sqlite()) as sqlite_connection:
        with postgres_connect(dsn) as postgres:
            create_postgres_schema(postgres)

            latest_round = postgres_max(postgres, "rounds", "round_id")
            round_rows = fetch_rounds(sqlite_connection, latest_round)
            sync_table(
                postgres, "rounds", "round_id", ROUND_COLUMNS, round_rows
            )

            available_round = postgres_max(postgres, "rounds", "round_id")
            latest_measurement = postgres_max(postgres, "measurement", "id")
            measurement_rows = fetch_measurements(
                sqlite_connection, latest_measurement, available_round
            )
            sync_table(
                postgres,
                "measurement",
                "id",
                MEASUREMENT_COLUMNS,
                measurement_rows,
            )

def main() -> None:
    dsn = load_postgres_dsn("tailscale")
    test_postgres_connection(dsn)

    with closing(connect_sqlite()) as sqlite_connection:
        with postgres_connect(dsn) as postgres:
            create_postgres_schema(postgres)

            latest_round = postgres_max(postgres, "rounds", "round_id")
            round_rows = fetch_rounds(sqlite_connection, latest_round)
            sync_table(
                postgres, "rounds", "round_id", ROUND_COLUMNS, round_rows
            )

            available_round = postgres_max(postgres, "rounds", "round_id")
            latest_measurement = postgres_max(postgres, "measurement", "id")
            measurement_rows = fetch_measurements(
                sqlite_connection, latest_measurement, available_round
            )
            sync_table(
                postgres,
                "measurement",
                "id",
                MEASUREMENT_COLUMNS,
                measurement_rows,
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(f"Sync failed: {error}") from error
