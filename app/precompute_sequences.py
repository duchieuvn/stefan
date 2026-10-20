#!/usr/bin/env python3
"""
Pre-computes and caches all temporal RTI sequences to disk.

Every available date within the last `config.history_months` months is
treated as a reference day; the full temporal sequence relative to it is
computed and saved to `sequence_cache/<date>.joblib`, overwriting old files.
The viewer (main.py) only loads these files.

With a PostgreSQL backend the per-day link statistics are aggregated on the
server in one query; with SQLite they are computed from the raw rows of a
local copy synced over SFTP.

Usage:
    python precompute_sequences.py [--db sqlite|postgres|pg-tailscale]
"""

import argparse
import logging
from dataclasses import replace
from datetime import timedelta

from config import DEFAULT_CONFIG, RTIConfig
from database import BACKENDS, DatabaseProvider, default_backend, ensure_local_database_current, open_database
from rti_temporal_core import (
    IntervalLoader,
    build_model_context,
    compute_temporal_sequence,
    prune_cache,
    save_cached_sequence,
    setup_logging,
)


def precompute_all(config: RTIConfig, database: DatabaseProvider) -> None:
    """Computes and saves the sequence for every reference date."""
    # Daily intervals are reused across all reference sequences.
    loader = IntervalLoader(database, config)
    dates = loader.available_dates()
    if not dates:
        logging.error("No usable dates found in the database. Aborting.")
        return

    logging.info("Found %d dates to process (%s … %s). Building RTI model context...", len(dates), dates[0], dates[-1])
    context = build_model_context(config)

    for i, ref_date in enumerate(dates, start=1):
        logging.info("Processing reference date %d/%d: %s", i, len(dates), ref_date)
        try:
            result = compute_temporal_sequence(context, loader, ref_date, dates)
            logging.info("Saved %s", save_cached_sequence(config, result))
        except Exception:
            logging.exception("Failed to compute or save sequence for ref_date %s.", ref_date)

    stale = prune_cache(config, dates)
    if stale:
        logging.info("Removed %d stale cache files (%s … %s).", len(stale), stale[0], stale[-1])
    logging.info("Pre-computation finished.")


def run(config: RTIConfig = DEFAULT_CONFIG, backend: str | None = None) -> None:
    """Pre-computes all sequences from `backend`. For SQLite, first makes sure the
    local database copy is fresh (syncing from the remote host if needed).
    `backend` defaults to RTI_DB_BACKEND from `.env`."""
    backend = backend or default_backend()
    if backend == "sqlite":
        db_path = ensure_local_database_current(config.db_path.parent, timedelta(hours=config.db_max_age_hours))
        config = replace(config, db_path=db_path)

    logging.info("Using %s backend.", backend)
    database = open_database(backend, config.db_path)
    try:
        precompute_all(config, database)
    finally:
        database.close()


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Pre-compute and cache RTI sequences to disk.")
    parser.add_argument(
        "--db",
        choices=BACKENDS,
        default=None,
        help="Database backend (default: RTI_DB_BACKEND from .env, else sqlite). 'sqlite' uses the newest local "
        "reduced_data_*.db, syncing a fresh one from the remote host first if missing or older than "
        f"{DEFAULT_CONFIG.db_max_age_hours:g} h; 'postgres' uses an SSH tunnel; 'pg-tailscale' connects directly "
        "(both aggregate on the server).",
    )
    args = parser.parse_args()
    run(DEFAULT_CONFIG, backend=args.db)


if __name__ == "__main__":
    main()
