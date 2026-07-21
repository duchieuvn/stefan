#!/usr/bin/env python3
"""Run the SQLite-to-PostgreSQL synchronization on a two-hour schedule."""

from contextlib import closing
from datetime import datetime
import time
from typing import Callable

import psycopg

try:
    from . import sync_postgres
except ImportError:  # Allow this file to be run directly from SOFTWARE/.
    import sync_postgres


RUN_INTERVAL_SECONDS = 2 * 60 * 60
MAX_RETRIES = 5
INITIAL_RETRY_DELAY_SECONDS = 5

DatabasePosition = tuple[int, int]


def log(message: str) -> None:
    """Print a timestamped scheduler message immediately."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


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
    dsn = sync_postgres.load_postgres_dsn()
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
    log(
        f"Sync progress: {format_position(position)}; "
        f"targets: {format_position(targets)}"
    )

    while not targets_reached(position, targets):
        previous_position = position
        sync_postgres.main()
        position = postgres_position()
        log(
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
                log(
                    "Starting scheduled sync; captured "
                    f"{format_position(targets)}"
                )
            catch_up_to(targets)
            log(f"Scheduled sync complete at {format_position(targets)}")
            return True
        except Exception as error:
            if attempt == MAX_RETRIES:
                log(
                    f"Scheduled sync failed after {MAX_RETRIES} retries: {error}"
                )
                return False

            delay = INITIAL_RETRY_DELAY_SECONDS * (2**attempt)
            log(
                f"Sync attempt {attempt + 1} failed: {error}; "
                f"retrying in {delay} seconds"
            )
            sleep(delay)

    return False


def main() -> None:
    """Run immediately, then wait two hours after each run completes."""
    log(
        "PostgreSQL sync scheduler started "
        f"(batch size={sync_postgres.BATCH_SIZE}, interval=2 hours)"
    )
    try:
        while True:
            run_scheduled_sync()
            log("Next scheduled sync will start in 2 hours")
            time.sleep(RUN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        log("PostgreSQL sync scheduler stopped")


if __name__ == "__main__":
    main()
