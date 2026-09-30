import argparse
import time

import schedule

from pump_water import run_pump_cycles

RUN_TIMES = ["09:50", "15:30"]

def main():

    parser = argparse.ArgumentParser(
        description="Run pump_water once a day at a fixed time."
    )
    parser.add_argument(
        "--cycles", type=int, default=100,
        help="Number of ON/OFF cycles per run, default 100"
    )
    args = parser.parse_args()


    print("Pump water daily scheduler started.")
    print("Press CTRL+C to stop.")

    for run_time in RUN_TIMES:
        schedule.every().day.at(run_time).do(run_pump_cycles, cycles=args.cycles)
        print(f"Pump water every day at {run_time}.")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)

    except KeyboardInterrupt:
        print()
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
