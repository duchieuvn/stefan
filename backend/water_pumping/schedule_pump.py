import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

import schedule

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/, for service_runtime
from service_runtime import ServiceContext
from pump_water import run_pump_cycles

RUN_TIMES = ["09:50", "15:30"]


def run(ctx: ServiceContext, cycles: int = 100) -> None:
    """Pump every day at RUN_TIMES (and on "run now") until ctx asks to stop."""

    def pump():
        started = datetime.now()
        ctx.report(activity=f"pumping ({cycles} cycles)")
        try:
            done = run_pump_cycles(cycles=cycles, ctx=ctx)
            result = "ok" if done == cycles else f"stopped after {done}/{cycles} cycles"
        except Exception as error:
            # Keep the scheduler alive; the next scheduled run tries again.
            traceback.print_exc()
            result = f"failed: {error}"
        ctx.report(activity="idle", last_run=started, last_result=result)

    print("Pump water daily scheduler started.")

    for run_time in RUN_TIMES:
        schedule.every().day.at(run_time).do(pump)
        print(f"Pump water every day at {run_time}.")
    ctx.report(activity="idle")

    while not ctx.stopping():
        schedule.run_pending()
        ctx.report(next_run=schedule.next_run())
        if ctx.idle(30):
            print("Run now requested.")
            pump()

    print("Scheduler stopped.")


def main():

    parser = argparse.ArgumentParser(
        description="Run pump_water once a day at a fixed time."
    )
    parser.add_argument(
        "--cycles", type=int, default=100,
        help="Number of ON/OFF cycles per run, default 100"
    )
    args = parser.parse_args()

    print("Press CTRL+C to stop.")

    try:
        run(ServiceContext.standalone(), cycles=args.cycles)

    except KeyboardInterrupt:
        print()
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
