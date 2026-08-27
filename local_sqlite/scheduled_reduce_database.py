#!/usr/bin/env python3
"""Run reduce_database.py on a twice-daily schedule."""

from datetime import datetime
import time
import schedule

try:
    from . import reduce_database
except ImportError:  # Allow this file to be run directly from SOFTWARE/.
    import reduce_database


RUN_TIMES = ("06:00", "18:00")


def log(message: str) -> None:
    """Print a timestamped scheduler message immediately."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def run_once() -> bool:
    """Run reduce_database once, returning whether it succeeded."""
    log("Starting reduce_database")
    try:
        reduce_database.reduce_database()
    except Exception as error:
        log(f"reduce_database failed: {error}")
        return False
    log("reduce_database finished successfully")
    return True


def main() -> None:
    """Run immediately, then run again at each scheduled time of day."""
    log(f"Reduce-database scheduler started (times={', '.join(RUN_TIMES)})")

    for run_time in RUN_TIMES:
        schedule.every().day.at(run_time).do(run_once)

    run_once()

    try:
        while True:
            schedule.run_pending()
            time.sleep(10)
    except KeyboardInterrupt:
        log("Reduce-database scheduler stopped")


if __name__ == "__main__":
    main()
