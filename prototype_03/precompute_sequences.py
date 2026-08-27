#!/usr/bin/env python3
"""
Pre-computes and caches all temporal RTI sequences to disk.

This script iterates through all available dates in the database, treats each
one as a reference day, computes the full temporal sequence relative to it,
and saves the result to a file on disk using joblib.

The main application (rti_10.py) can then load these pre-computed files
instead of performing the expensive calculations itself.

Usage:
    python precompute_sequences.py
    python precompute_sequences.py --force  # Overwrite existing cache files
"""
import argparse
import logging
import sqlite3
from datetime import date
import calendar
from pathlib import Path

import joblib

from config import DEFAULT_CONFIG, RTIConfig
from database import DatabaseProvider, PostgresDatabase, PostgresTunnelDatabase, SQLiteDatabase
from rti_temporal_core import (
    build_model_context,
    compute_temporal_sequence,
    get_available_dates,
)
import sync_remote_sqlite

CONFIG = DEFAULT_CONFIG


def _get_local_latest_date(db_path: Path) -> str | None:
    """Returns the most recent measurement date (YYYY-MM-DD) in the local SQLite file."""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        row = con.execute(
            "SELECT MAX(substr(pc_ts, 1, 10)) FROM measurement WHERE pc_ts IS NOT NULL"
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def ensure_local_database_current(db_path: Path) -> None:
    """Downloads the remote reduced database if `db_path` is missing or not from today."""
    if not db_path.exists():
        logging.info("Local SQLite database not found at %s. Fetching from remote...", db_path)
        sync_remote_sqlite.sync_remote_database(db_path)
        return

    today = date.today().isoformat()
    latest = _get_local_latest_date(db_path)
    if latest == today:
        logging.info("Local SQLite database is current (latest date: %s).", latest)
        return

    logging.info(
        "Local SQLite database is stale (latest date: %s, today: %s). Fetching from remote...",
        latest,
        today,
    )
    sync_remote_sqlite.sync_remote_database(db_path)


def _get_cutoff_date(latest_date: date, months_back: int) -> date:
    """Calculates the date `months_back` months before `latest_date`."""
    year = latest_date.year
    month = latest_date.month - months_back
    day = latest_date.day

    # Normalize month and year
    while month <= 0:
        month += 12
        year -= 1

    # Adjust day if it's out of range for the target month
    # e.g., calculating 1 month before March 31st should be Feb 28/29.
    last_day_of_month = calendar.monthrange(year, month)[1]
    day = min(day, last_day_of_month)

    return date(year, month, day)


def precompute_all(config: RTIConfig, database: DatabaseProvider, force: bool = False) -> None:
    """
    Computes and saves all possible temporal sequences.
    """
    cache_dir = config.sequence_cache_dir
    cache_dir.mkdir(exist_ok=True)
    logging.info("Using on-disk cache directory: %s", cache_dir)

    logging.info("Connecting to database...")
    con = database.connect()
    try:
        logging.info("Fetching available dates.")
        available_dates = get_available_dates(con, config)
    finally:
        con.close()

    if not available_dates:
        logging.error("No usable dates found in the database. Aborting.")
        return

    # Hardcode filtering to the last 6 months of data.
    months_to_keep = 3
    logging.info("Filtering dates to the last %d months.", months_to_keep)
    latest_date = date.fromisoformat(max(available_dates))
    cutoff_date = _get_cutoff_date(latest_date, months_to_keep)

    original_count = len(available_dates)
    available_dates = [d for d in available_dates if date.fromisoformat(d) >= cutoff_date]
    logging.info(
        "Filtered from %d to %d dates (from %s to %s).",
        original_count,
        len(available_dates),
        cutoff_date.isoformat(),
        latest_date.isoformat(),
    )

    if not available_dates:
        logging.error("No usable dates found in the selected date range. Aborting.")
        return

    logging.info("Found %d available dates to process.", len(available_dates))
    logging.info("Building RTI model context...")
    context = build_model_context(config)

    # This in-memory cache speeds up the pre-computation itself, as many
    # daily intervals are reused across different reference sequences.
    interval_cache: dict[tuple[str, str], tuple[dict, dict]] = {}

    for i, ref_date in enumerate(available_dates):
        output_file = cache_dir / f"{ref_date}.joblib"
        logging.info("Processing reference date %d/%d: %s", i + 1, len(available_dates), ref_date)

        try:
            logging.info("Computing temporal sequence for reference date %s...", ref_date)
            result = compute_temporal_sequence(
                context=context,
                ref_date=ref_date,
                sequence_dates=available_dates,
                interval_cache=interval_cache,
                database=database,
                config=config,
            )

            logging.info("Saving sequence to %s...", output_file)
            joblib.dump(result, output_file, compress=3)
            logging.info("... done.")

        except Exception:
            logging.exception("Failed to compute or save sequence for ref_date %s.", ref_date)

    database.close()
    logging.info("Pre-computation finished.")


def run(config: RTIConfig = CONFIG, force: bool = False) -> None:
    """Programmatic entry point: ensures the local SQLite database is current,
    then computes all sequences from it. Used by main.py to auto-refresh data."""
    ensure_local_database_current(config.db_path)
    database = SQLiteDatabase.from_config(config)
    precompute_all(config=config, database=database, force=force)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    parser = argparse.ArgumentParser(description="Pre-compute and cache RTI sequences to disk.")
    parser.add_argument(
        "--db",
        choices=["sqlite", "postgres", "pg-tailscale"],
        default="sqlite",
        help="Database backend to use. 'sqlite' uses the local reduced_data.db (syncing it from the "
        "remote Tailscale host first if missing or stale), 'postgres' uses an SSH tunnel, "
        "'pg-tailscale' connects directly (default: sqlite).",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing cache files.")
    args = parser.parse_args()

    if args.db == "sqlite":
        logging.info("Using SQLite backend.")
        run(config=CONFIG, force=args.force)
        return

    database: DatabaseProvider
    if args.db == "postgres":
        logging.info("Using PostgreSQL backend.")
        database = PostgresTunnelDatabase.from_environment()
    else:
        logging.info("Using direct Tailscale PostgreSQL backend.")
        database = PostgresDatabase.from_environment()

    precompute_all(config=CONFIG, database=database, force=args.force)

if __name__ == "__main__":
    main()