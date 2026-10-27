"""Settings for backend/services/sensor_logger, loaded from its .env.

.env is a symlink to .env.prod or .env.test. On the first run (no .env yet) the
choice is asked on the terminal; without a terminal it fails instead of guessing.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager"))  # for db_path
import db_path

MASTER_DIR = Path(__file__).resolve().parent
ENV_PATH = MASTER_DIR / ".env"
ENV_CHOICES = ("prod", "test")


def ask_env() -> str:
    """Ask on the terminal which hardware config to use: "prod" or "test"."""
    print(f"{ENV_PATH} does not exist yet. Which config should the sensor logger use?")
    print("  prod = .env.prod (production hardware)\n  test = .env.test (test hardware)")
    while True:
        answer = input("Config [prod/test]: ").strip().lower()
        if answer in ENV_CHOICES:
            return answer
        print("Please answer prod or test.")


def link_env(choice: str) -> None:
    """Point .env at .env.<choice>."""
    ENV_PATH.symlink_to(f".env.{choice}")
    print(f"{ENV_PATH} -> .env.{choice}")


if not ENV_PATH.exists() and not ENV_PATH.is_symlink():
    if not sys.stdin.isatty():
        raise RuntimeError(f"{ENV_PATH} does not exist. Choose the config once: run "
                           f"main_senor_data.py in a terminal, or ln -s .env.prod (or .env.test) {ENV_PATH}")
    link_env(ask_env())
load_dotenv(ENV_PATH)
env = os.environ

# Serial / storage
PORT = env["PORT"]
BAUDRATE = int(env["BAUDRATE"])
DB_PATH = str(db_path.sqlite_path())   # backend/data_path.env, asked on the first run
DATA_DIR = Path(DB_PATH).parent

NODES = [int(n) for n in env["NODES"].split(",")]

# Register map
CMD_REGISTER = int(env["CMD_REGISTER"])
CMD_TX = int(env["CMD_TX"])
CMD_LED_ON = int(env["CMD_LED_ON"])
CMD_LED_OFF = int(env["CMD_LED_OFF"])

TX_TIMESTAMP_BASE_REG = int(env["TX_TIMESTAMP_BASE_REG"])

RX_BLOCK_ADDR = int(env["RX_BLOCK_ADDR"])
RX_BLOCK_COUNT = int(env["RX_BLOCK_COUNT"])

# Payload
PAYLOAD_LEN_REG = int(env["PAYLOAD_LEN_REG"])
PAYLOAD_DATA_BASE = int(env["PAYLOAD_DATA_BASE"])
PAYLOAD_MAX_BYTES = int(env["PAYLOAD_MAX_BYTES"])
PAYLOAD_TEXT = env["PAYLOAD_TEXT"]

# Timing
RX_TIMEOUT_S = float(env["RX_TIMEOUT_S"])
RX_POLL_S = float(env["RX_POLL_S"])
RX_STABLE_READS = int(env["RX_STABLE_READS"])
RX_STABLE_GAP_S = float(env["RX_STABLE_GAP_S"])

MEASURE_INTERVAL_S = float(env["MEASURE_INTERVAL_S"])

# SQLite robustness
SQLITE_CONNECT_TIMEOUT_S = float(env["SQLITE_CONNECT_TIMEOUT_S"])
SQLITE_BUSY_TIMEOUT_MS = int(env["SQLITE_BUSY_TIMEOUT_MS"])
COMMIT_RETRIES = int(env["COMMIT_RETRIES"])
COMMIT_BACKOFF_START_S = float(env["COMMIT_BACKOFF_START_S"])
COMMIT_BACKOFF_MAX_S = float(env["COMMIT_BACKOFF_MAX_S"])

# LoRa config registers
LORA_BASE = int(env["LORA_BASE"])
LORA_APPLY = int(env["LORA_APPLY"])

# LoRa defaults
DEFAULT_FREQ_MHZ = float(env["DEFAULT_FREQ_MHZ"])
DEFAULT_POWER = int(env["DEFAULT_POWER"])
DEFAULT_SF = int(env["DEFAULT_SF"])
DEFAULT_BW = int(env["DEFAULT_BW"])
DEFAULT_CR = int(env["DEFAULT_CR"])

# Terminal logging
VERBOSE = env["VERBOSE"] == "true"
PRINT_LINK_LINE = env["PRINT_LINK_LINE"] == "true"

# Firmware update (OTA, see ota_update.py). Optional keys: an older .env still works.
OTA_PORT = env.get("OTA_PORT", PORT)
# One app image ("Export Compiled Binary") for all nodes; each node keeps its ID in NVS
OTA_URL = env.get("OTA_URL", "https://github.com/duchieuvn/stefan/releases/latest/download/slave.bin")
OTA_NODE_TIMEOUT_S = float(env.get("OTA_NODE_TIMEOUT_S", "300"))
