import argparse
import time

import schedule

from pump_water import run_pump_cycles

RUN_TIME = "07:00"


def main():

    parser = argparse.ArgumentParser(
        description="Run pump_water once a day at a fixed time."
    )
    parser.add_argument(
        "--cycles", type=int, default=100,
        help="Number of ON/OFF cycles per run, default 100"
    )
    args = parser.parse_args()


    schedule.every().day.at(RUN_TIME).do(run_pump_cycles, cycles=args.cycles)
    print("Pump water daily scheduler started.")
    print(f"Runs every day at {RUN_TIME}.")
    print("Press CTRL+C to stop.")

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)

    except KeyboardInterrupt:
        print()
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
