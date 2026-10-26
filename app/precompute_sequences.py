#!/usr/bin/env python3
"""
Pre-computes and caches all temporal RTI sequences to disk.

Every available date within the last `config.history_months` months is
treated as a reference day; the full temporal sequence relative to it is
computed and saved to `sequence_cache/<date>.joblib`, overwriting old files.
The viewer (main.py) only loads these files.

The link statistics are computed from the raw rows of a local SQLite file.

Usage:
    python precompute_sequences.py path/to/data.db
"""

import argparse
import logging
from pathlib import Path

from config import DEFAULT_CONFIG, RTIConfig
from database import SQLiteDatabase
from rti_temporal_core import (
    IntervalLoader,
    build_model_context,
    compute_temporal_sequence,
    config_for_database,
    prune_cache,
    save_cached_sequence,
    setup_logging,
)


def precompute_all(config: RTIConfig, database: SQLiteDatabase) -> None:
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


def run(db_path: Path, config: RTIConfig = DEFAULT_CONFIG) -> RTIConfig:
    """Pre-computes all sequences from the SQLite file `db_path`.
    Returns the config pointing at that database and its cache folder."""
    config = config_for_database(config, db_path)
    logging.info("Using database %s.", config.db_path)
    precompute_all(config, SQLiteDatabase(config.db_path))
    return config


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="Pre-compute and cache RTI sequences to disk.")
    parser.add_argument("db", type=Path, help="Path to the local SQLite .db file.")
    args = parser.parse_args()
    run(args.db)


if __name__ == "__main__":
    main()
