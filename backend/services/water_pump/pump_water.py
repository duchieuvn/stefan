import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager"))  # for service_runtime
from service_runtime import ServiceContext

# ============================
# Heltec settings
# ============================

PORT = "/dev/ttyUSB1"
BAUDRATE = 115200

ON_TIME = 12      # Pump ON for 13 seconds
OFF_TIME = 30     # Pump OFF for 30 seconds


# ============================
# Send command
# ============================

def send_command(heltec, command):

    heltec.write((command + "\n").encode())

    time.sleep(0.2)

    response = heltec.readline().decode(
        errors="ignore"
    ).strip()

    return response


# ============================
# Countdown function
# ============================

def countdown(seconds, status, ctx):
    """Wait `seconds` showing the time left; return True if ctx asked to stop first."""

    end_time = time.time() + seconds

    while True:

        remaining = end_time - time.time()

        if remaining <= 0:
            break

        # Display whole seconds remaining
        remaining_seconds = int(remaining) + 1

        print(
            f"\r{status} | Time remaining: {remaining_seconds:02d} seconds",
            end="",
            flush=True
        )

        if ctx.wait(0.1):
            print()
            return True

    print(
        f"\r{status} | Time remaining: 00 seconds"
    )

    return False


# ============================
# Main pump routine
# ============================

def run_pump_cycles(cycles=100, port=PORT, baudrate=BAUDRATE, on_time=ON_TIME, off_time=OFF_TIME, ctx=None):
    """Run ON/OFF cycles until done, Ctrl+C or a stop request; return the number of completed cycles."""

    ctx = ctx or ServiceContext.standalone()

    print(f"Connecting to Heltec on {port}...")
    print(f"Current date and time: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    heltec = serial.Serial(
        port,
        baudrate,
        timeout=2
    )

    # Opening the port resets the Heltec
    time.sleep(2)

    # Clear startup messages
    heltec.reset_input_buffer()

    print("Connected to Heltec!")
    print()
    print(f"Pump ON  = {on_time} seconds")
    print(f"Pump OFF = {off_time} seconds")
    print()
    print("Press CTRL+C to stop.")
    print()

    cycle = 1

    try:

        while cycle <= cycles:

            # --------------------
            # Pump ON
            # --------------------

            print()
            print("==============================")
            print(f"Cycle {cycle}")
            print("PUMP ON")
            ctx.report(activity=f"cycle {cycle}/{cycles}: pump ON")

            response = send_command(heltec, "ON")

            print("Heltec:", response)

            if countdown(on_time, "PUMP ON ", ctx):
                break

            # --------------------
            # Pump OFF
            # --------------------

            print()
            print("PUMP OFF")
            ctx.report(activity=f"cycle {cycle}/{cycles}: pump OFF")

            response = send_command(heltec, "OFF")

            print("Heltec:", response)

            if countdown(off_time, "PUMP OFF", ctx):
                break

            cycle += 1

    # ============================
    # Stop safely with CTRL+C
    # ============================

    except KeyboardInterrupt:

        print()
        print()

    finally:

        # Stopped early (Ctrl+C, stop request or an error): make sure the pump is OFF
        if cycle <= cycles:

            print("Stopping program...")
            print("Turning pump OFF...")

            try:

                response = send_command(heltec, "OFF")
                print("Heltec:", response)

            except Exception:
                pass

            print("Pump OFF.")

        heltec.close()

    return cycle - 1


if __name__ == "__main__":
    run_pump_cycles(cycles=100)
