"""Settings for backend/master, loaded from backend/master/.env (falls back to .env.test)."""
import os
from pathlib import Path

from dotenv import load_dotenv

MASTER_DIR = Path(__file__).resolve().parent
ENV_PATH = MASTER_DIR / ".env" if (MASTER_DIR / ".env").exists() else MASTER_DIR / ".env.test"
load_dotenv(ENV_PATH)
env = os.environ

# Serial / storage
PORT = env["PORT"]
BAUDRATE = int(env["BAUDRATE"])
DATA_DIR = MASTER_DIR.parent / env["DATA_DIR"]   # relative to backend/
DB_PATH = str(DATA_DIR / env["DB_FILENAME"])

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
