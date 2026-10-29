"""Settings for backend/services/slave_rotation, loaded from its own .env.

Copy .env.example to .env and adjust it (PORT, NODES, ...). Nothing here is shared with
services/sensor_logger: this folder is self-contained. Only the database file is the same,
so the rounds end up next to the data of the other loggers.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parents[1]
ENV_PATH = HERE / ".env"
DATA_PATH_ENV = BACKEND_DIR / "data_path.env"   # SQLITE_PATH=..., written by manage_services.py

if not ENV_PATH.exists():
    raise RuntimeError(f"{ENV_PATH} does not exist. Copy {HERE / '.env.example'} to {ENV_PATH} "
                       f"and set PORT and NODES (same values as in services/sensor_logger/.env).")
load_dotenv(ENV_PATH)
env = os.environ


def sqlite_path() -> Path:
    """The sensor database: SQLITE_PATH from .env, else from backend/data_path.env.

    Relative paths are relative to backend/.
    """
    value = env.get("SQLITE_PATH", "").strip()
    if not value and DATA_PATH_ENV.exists():
        for line in DATA_PATH_ENV.read_text().splitlines():
            key, sep, rest = line.partition("=")
            if sep and key.strip() == "SQLITE_PATH" and rest.strip():
                value = rest.strip().strip("\"'")
                break
    if not value:
        raise RuntimeError(f"No database path: set SQLITE_PATH in {ENV_PATH}, or create {DATA_PATH_ENV} "
                           f"(it is written when the sensor service is started once in a terminal).")
    path = Path(value).expanduser()
    return path if path.is_absolute() else BACKEND_DIR / path


# Serial / storage
PORT = env["PORT"]
BAUDRATE = int(env.get("BAUDRATE", "9600"))
DB_PATH = str(sqlite_path())
DATA_DIR = Path(DB_PATH).parent

NODES = [int(n) for n in env["NODES"].split(",")]

# LoRa round: payload and timing
PAYLOAD_TEXT = env.get("PAYLOAD_TEXT", "")
RX_TIMEOUT_S = float(env.get("RX_TIMEOUT_S", "5"))
RX_POLL_S = float(env.get("RX_POLL_S", "0.2"))
RX_STABLE_READS = int(env.get("RX_STABLE_READS", "2"))
RX_STABLE_GAP_S = float(env.get("RX_STABLE_GAP_S", "0.05"))

# Pause between two rounds (LoRa or CW): a round starts every MEASURE_INTERVAL_S
MEASURE_INTERVAL_S = float(env.get("MEASURE_INTERVAL_S", "60"))

# CW round
CW_MEASURE_MS = int(env.get("CW_MEASURE_MS", "1000"))       # so lange misst jeder Empfänger
CW_TX_MAX_MS = int(env.get("CW_TX_MAX_MS", "10000"))        # Firmware-Limit (CW_MAX_TX_MS)
CW_RESULT_TIMEOUT_S = float(env.get("CW_RESULT_TIMEOUT_S", "3"))
CW_POLL_S = float(env.get("CW_POLL_S", "0.1"))

# Rotation: LORA_ROUNDS lora rounds, then CW_ROUNDS cw rounds, then again ...
LORA_ROUNDS = int(env.get("LORA_ROUNDS", "1"))
CW_ROUNDS = int(env.get("CW_ROUNDS", "1"))

# Mode switch: wait this long for all nodes to report the new mode (register 421), then retry
MODE_SWITCH_TIMEOUT_S = float(env.get("MODE_SWITCH_TIMEOUT_S", "5"))
MODE_SWITCH_RETRIES = int(env.get("MODE_SWITCH_RETRIES", "3"))
MODE_POLL_S = float(env.get("MODE_POLL_S", "0.1"))
MODE_SETTLE_S = float(env.get("MODE_SETTLE_S", "0.3"))      # nach dem Umschalten, bevor die Runde startet

# EU 433 MHz: 10 % duty cycle; the service warns if the CW rounds could exceed it
DUTY_CYCLE_LIMIT = float(env.get("DUTY_CYCLE_LIMIT", "0.10"))

# Radio defaults, used when the nodes' registers can't be read
DEFAULT_FREQ_MHZ = float(env.get("DEFAULT_FREQ_MHZ", "433.0"))
DEFAULT_POWER = int(env.get("DEFAULT_POWER", "22"))
DEFAULT_SF = int(env.get("DEFAULT_SF", "7"))
DEFAULT_BW = int(env.get("DEFAULT_BW", "125"))
DEFAULT_CR = int(env.get("DEFAULT_CR", "5"))

# SQLite robustness
SQLITE_CONNECT_TIMEOUT_S = float(env.get("SQLITE_CONNECT_TIMEOUT_S", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(env.get("SQLITE_BUSY_TIMEOUT_MS", "15000"))
COMMIT_RETRIES = int(env.get("COMMIT_RETRIES", "8"))
COMMIT_BACKOFF_START_S = float(env.get("COMMIT_BACKOFF_START_S", "0.2"))
COMMIT_BACKOFF_MAX_S = float(env.get("COMMIT_BACKOFF_MAX_S", "3"))

# Terminal logging
VERBOSE = env.get("VERBOSE", "true") == "true"
PRINT_LINK_LINE = env.get("PRINT_LINK_LINE", "true") == "true"
