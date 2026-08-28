import serial
import time

# ============================
# Heltec settings
# ============================

PORT = "/dev/ttyUSB1"
BAUDRATE = 115200

ON_TIME = 13      # Pump ON for 13 seconds
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

def countdown(seconds, status):

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

        time.sleep(0.1)

    print(
        f"\r{status} | Time remaining: 00 seconds"
    )


# ============================
# Main pump routine
# ============================

def run_pump_cycles(cycles=100, port=PORT, baudrate=BAUDRATE, on_time=ON_TIME, off_time=OFF_TIME):

    print(f"Connecting to Heltec on {port}...")

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

            response = send_command(heltec, "ON")

            print("Heltec:", response)

            countdown(
                on_time,
                "PUMP ON "
            )

            # --------------------
            # Pump OFF
            # --------------------

            print()
            print("PUMP OFF")

            response = send_command(heltec, "OFF")

            print("Heltec:", response)

            countdown(
                off_time,
                "PUMP OFF"
            )

            cycle += 1

    # ============================
    # Stop safely with CTRL+C
    # ============================

    except KeyboardInterrupt:

        print()
        print()
        print("Stopping program...")
        print("Turning pump OFF...")

        try:

            response = send_command(heltec, "OFF")
            print("Heltec:", response)

        except Exception:
            pass

        print("Pump OFF.")
        print("Program stopped safely.")

    finally:
        heltec.close()


if __name__ == "__main__":
    run_pump_cycles(cycles=100)
