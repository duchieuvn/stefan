#!/usr/bin/env python3
"""Run reduce_database.py on a twice-daily schedule."""

from datetime import datetime
from pathlib import Path
import sys
import schedule

try:
    from . import reduce_database
except ImportError:  # Allow this file to be run directly from SOFTWARE/.
    import reduce_database

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/, for service_runtime
from service_runtime import ServiceContext


RUN_TIMES = ("06:00", "18:00")


def log(message: str) -> None:
    """Print a timestamped scheduler message immediately."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def run_once(ctx: ServiceContext) -> bool:
    """Run reduce_database once, returning whether it succeeded."""
    started = datetime.now()
    log("Starting reduce_database")
    ctx.report(activity="reducing")
    try:
        reduce_database.reduce_database()
    except (Exception, SystemExit) as error:  # it exits when the source database is missing
        log(f"reduce_database failed: {error}")
        result = f"failed: {error}"
    else:
        log("reduce_database finished successfully")
        result = "ok"
    ctx.report(activity="idle", last_run=started, last_result=result)
    return result == "ok"


def run(ctx: ServiceContext) -> None:
    """Run immediately, then at each scheduled time of day (and on "run now") until ctx asks to stop."""
    log(f"Reduce-database scheduler started (times={', '.join(RUN_TIMES)})")

    for run_time in RUN_TIMES:
        schedule.every().day.at(run_time).do(run_once, ctx)

    run_once(ctx)

    while not ctx.stopping():
        schedule.run_pending()
        ctx.report(next_run=schedule.next_run())
        if ctx.idle(10):
            log("Run now requested")
            run_once(ctx)

    log("Reduce-database scheduler stopped")


def main() -> None:
    try:
        run(ServiceContext.standalone())
    except KeyboardInterrupt:
        log("Reduce-database scheduler stopped")


if __name__ == "__main__":
    main()
