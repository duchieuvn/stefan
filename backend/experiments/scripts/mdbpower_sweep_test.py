from __future__ import annotations

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
import csv
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

ROOT = Path(__file__).resolve().parents[2]   # backend/


# =========================
# CONFIG (adjust as needed!)
# =========================
# Node bus is on the FTDI adapter; by-id path survives ttyUSB renumbering
PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0"
BAUDRATE = 9600
DB_PATH = ROOT / "data/power_sweep/power_data_sweep.db"   # separate DB, so manual lora_logger_03 runs don't get mixed in

# In addition to the SQLite DB, a separate CSV file is written per
# configuration (for tools/colleagues without SQL, and for analyze_sweep_duty_cycle.py).
CSV_OUT_DIR = ROOT / "data/power_sweep/runs_power_csv"
MANIFEST_CSV_PATH = CSV_OUT_DIR / "runs_manifest.csv"

NODES = [1, 2, 3, 4, 5, 6, 7, 8]

# ---- Automated sweep --------------------------------------------------
# Each combination is written to all NODES via Modbus (no re-flash
# needed). The configs are interleaved: one round with config 1, one round
# with config 2, ... up to the last config, then the pass repeats until every
# config has RUN_MAX_ROUNDS rounds. Round starts are spaced MEASURE_INTERVAL_S
# apart (config switch included). Unlike lora_logger_03.py (which asks interactively once
# and then runs until Ctrl+C), this script runs through the entire list
# unattended.
SWEEP_CONFIGS: List[Dict[str, Any]] = [
    {"freq_mhz": 433.1, "tx_power": 0 },
    {"freq_mhz": 433.1, "tx_power": 1 },
    {"freq_mhz": 433.1, "tx_power": 2 },
    {"freq_mhz": 433.1, "tx_power": 3 },
    {"freq_mhz": 433.1, "tx_power": 4 },
    {"freq_mhz": 433.1, "tx_power": 5 },
    {"freq_mhz": 433.1, "tx_power": 6 },
    {"freq_mhz": 433.1, "tx_power": 7 },
    {"freq_mhz": 433.1, "tx_power": 8 },
    {"freq_mhz": 433.1, "tx_power": 9 },
    {"freq_mhz": 433.1, "tx_power": 10 },
    {"freq_mhz": 433.1, "tx_power": 11 },
    {"freq_mhz": 433.1, "tx_power": 12 },
    {"freq_mhz": 433.1, "tx_power": 13 },
    {"freq_mhz": 433.1, "tx_power": 14 },
    {"freq_mhz": 433.1, "tx_power": 15 },
    {"freq_mhz": 433.1, "tx_power": 16 },
    {"freq_mhz": 433.1, "tx_power": 17 },
    {"freq_mhz": 433.1, "tx_power": 18 },
    {"freq_mhz": 433.1, "tx_power": 19 },
    {"freq_mhz": 433.1, "tx_power": 20 },
    {"freq_mhz": 433.1, "tx_power": 21 },
    {"freq_mhz": 433.1, "tx_power": 22 }
]

# Applied to every SWEEP_CONFIGS entry that doesn't set these keys itself
SWEEP_DEFAULTS: Dict[str, Any] = {"sf": 12, "bw_khz": 125, "cr_denom": 8}
SWEEP_CONFIGS = [{**SWEEP_DEFAULTS, **c} for c in SWEEP_CONFIGS]

RUN_MAX_ROUNDS = 2000      # rounds per configuration (only stop condition)
MEASURE_INTERVAL_S = 2.0   # spacing between round starts (duty-cycle control)
CONFIG_SETTLE_S = 1.0    # wait time after switching, until the radio is stable

# Register map
CMD_REGISTER   = 200
CMD_TX = 10
CMD_BME = 11
CMD_LED_ON = 1
CMD_LED_OFF = 2

# 300/301: 32-bit TX timestamp (ms), sent along by the sender
# and returned 1:1 by the receiver in the RX block
TX_TIMESTAMP_BASE_REG = 300

RX_BLOCK_ADDR = 100
RX_BLOCK_COUNT = 16

BME_TEMP_REG = 120
BME_NODES = {5, 6, 7, 8}

# Payload
PAYLOAD_LEN_REG = 320
PAYLOAD_DATA_BASE = 321
PAYLOAD_MAX_BYTES = 64
PAYLOAD_TEXT = "The quick brown fox jumps over the lazy dog"

# Timing
RX_TIMEOUT_S = 10.0
RX_POLL_S = 0.05
RX_STABLE_READS = 2
RX_STABLE_GAP_S = 0.03

BME_TIMEOUT_S = 3.0
BME_POLL_S = 0.2
REQUIRE_BME = False

# SQLite robustness
SQLITE_CONNECT_TIMEOUT_S = 30.0
SQLITE_BUSY_TIMEOUT_MS = 15000
COMMIT_RETRIES = 12
COMMIT_BACKOFF_START_S = 0.15
COMMIT_BACKOFF_MAX_S = 2.0

# LoRa config registers
# 400..404 = freq_x10, power, sf, bw, cr
LORA_BASE = 400
LORA_APPLY = 410

# Terminal logging
VERBOSE = True
PRINT_LINK_LINE = True


# =========================
# SQLite helpers
# =========================
MEASUREMENT_TABLE = "measurement"
ROUNDS_TABLE = "rounds"


def connect_db(db_path: str, timeout_s: float = 30.0) -> sqlite3.Connection:
    return sqlite3.connect(db_path, check_same_thread=False, timeout=float(timeout_s))


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def init_db(db_path: str) -> None:
    con = connect_db(db_path, timeout_s=30.0)
    cur = con.cursor()

    cur.execute("PRAGMA foreign_keys = ON;")
    cur.execute("PRAGMA busy_timeout = 15000;")
    try:
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = NORMAL;")
    except Exception:
        pass

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(MEASUREMENT_TABLE)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc_ts TEXT NOT NULL,

            round_id INTEGER,

            sender_id INTEGER,
            receiver_id INTEGER,

            marker INTEGER,
            payload_len INTEGER,
            payload_text TEXT,

            trigger_ms REAL,
            rx_wait_ms REAL,
            e2e_ms REAL,

            ok_match INTEGER,

            rx_id INTEGER,
            node_timestamp INTEGER,

            rssi REAL,
            snr REAL,

            bme_wait_ms REAL,
            bme_temp REAL,
            bme_hum REAL,
            bme_press REAL
        );
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(ROUNDS_TABLE)} (
            round_id INTEGER PRIMARY KEY,

            pc_start TEXT,
            pc_end TEXT,
            duration_s REAL,

            nodes_csv TEXT,
            payload_len INTEGER,
            payload_text TEXT,

            ok_links INTEGER,
            total_links INTEGER,
            timeouts INTEGER,

            freq_mhz REAL,
            tx_power INTEGER,
            sf INTEGER,
            bw_khz INTEGER,
            cr_denom INTEGER,
            lora_applied INTEGER,

            port TEXT,
            baudrate INTEGER,
            measure_interval_s REAL,

            notes TEXT
        );
    """)

    con.commit()
    con.close()


def store_measurement(cur: sqlite3.Cursor, row: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT INTO {quote_ident(MEASUREMENT_TABLE)} (
            pc_ts,
            round_id, sender_id, receiver_id,
            marker, payload_len, payload_text,
            trigger_ms, rx_wait_ms, e2e_ms,
            ok_match, rx_id, node_timestamp, rssi, snr,
            bme_wait_ms, bme_temp, bme_hum, bme_press
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        row.get("pc_ts"),
        row.get("round_id"), row.get("sender_id"), row.get("receiver_id"),
        row.get("marker"), row.get("payload_len"), row.get("payload_text"),
        row.get("trigger_ms"), row.get("rx_wait_ms"), row.get("e2e_ms"),
        row.get("ok_match"), row.get("rx_id"), row.get("node_timestamp"),
        row.get("rssi"), row.get("snr"),
        row.get("bme_wait_ms"), row.get("bme_temp"), row.get("bme_hum"), row.get("bme_press"),
    ))


def store_round(cur: sqlite3.Cursor, info: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT OR REPLACE INTO {quote_ident(ROUNDS_TABLE)} (
            round_id,
            pc_start, pc_end, duration_s,

            nodes_csv,
            payload_len, payload_text,

            ok_links, total_links, timeouts,

            freq_mhz, tx_power, sf, bw_khz, cr_denom, lora_applied,

            port, baudrate, measure_interval_s, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        info.get("round_id"),
        info.get("pc_start"), info.get("pc_end"), info.get("duration_s"),

        info.get("nodes_csv"),
        info.get("payload_len"), info.get("payload_text"),

        info.get("ok_links"), info.get("total_links"), info.get("timeouts"),

        info.get("freq_mhz"), info.get("tx_power"), info.get("sf"),
        info.get("bw_khz"), info.get("cr_denom"), info.get("lora_applied"),

        info.get("port"), info.get("baudrate"), info.get("measure_interval_s"),
        info.get("notes"),
    ))


def safe_commit(conn: sqlite3.Connection):
    backoff = COMMIT_BACKOFF_START_S
    for _ in range(COMMIT_RETRIES):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" in msg or "busy" in msg:
                time.sleep(backoff)
                backoff = min(COMMIT_BACKOFF_MAX_S, backoff * 1.7)
                continue
            raise
    raise sqlite3.OperationalError("DB busy/locked too long (commit retries exceeded)")


# =========================
# CSV helpers (parallel to the SQLite DB, see CSV_OUT_DIR)
# =========================
MEASUREMENT_CSV_FIELDS = [
    "pc_ts", "round_id", "sender_id", "receiver_id",
    "freq_mhz", "tx_power", "sf", "bw_khz", "cr_denom",
    "payload_len",
    "trigger_ms", "rx_wait_ms", "e2e_ms",
    "ok_match", "rx_id", "node_timestamp", "rssi", "snr",
    "bme_temp", "bme_hum", "bme_press",
]

MANIFEST_CSV_FIELDS = [
    "config_id", "freq_mhz", "tx_power", "sf", "bw_khz", "cr_denom",
    "csv_file", "start_ts", "end_ts", "duration_s",
    "rounds_completed", "total_links", "ok_links",
]


def init_csv(path: Path, fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def append_csv_row(path: Path, fields: List[str], row: Dict[str, Any]) -> None:
    with path.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writerow(row)


def fmt_freq(freq_mhz: float) -> str:
    return f"{float(freq_mhz):.1f}".replace(".", "p")


def fmt_bw(bw_khz: float) -> str:
    return f"{float(bw_khz):.0f}"


def config_csv_path(cfg: Dict[str, Any]) -> Path:
    return CSV_OUT_DIR / (
        f"results_freq{fmt_freq(cfg['freq_mhz'])}MHz_pwr{int(cfg['tx_power'])}dBm_"
        f"sf{int(cfg['sf'])}_bw{fmt_bw(cfg['bw_khz'])}kHz_cr{int(cfg['cr_denom'])}.csv"
    )


# =========================
# Generic helpers
# =========================
def log(msg: str) -> None:
    print(msg, flush=True)


def to_int16(val: int) -> int:
    return val if val < 32768 else val - 65536


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _call(func, *args, device_id=None, **kwargs):
    """
    pymodbus compatibility: device_id vs slave
    """
    if device_id is None:
        return func(*args, **kwargs)
    try:
        return func(*args, device_id=device_id, **kwargs)
    except TypeError:
        return func(*args, slave=device_id, **kwargs)


_last_tx_timestamp = -1

def make_tx_timestamp_31() -> int:
    global _last_tx_timestamp

    while True:
        ts = int(time.monotonic_ns() // 1_000_000) & 0x7FFFFFFF
        if ts != _last_tx_timestamp:
            _last_tx_timestamp = ts
            return ts
        time.sleep(0.001)

def pack_ascii_to_regs(text: str, max_len: int = PAYLOAD_MAX_BYTES) -> Tuple[int, List[int]]:
    b = text.encode("ascii", "ignore")[:max_len]
    regs: List[int] = []
    for i in range(0, len(b), 2):
        hi = b[i]
        lo = b[i + 1] if i + 1 < len(b) else 0
        regs.append((hi << 8) | lo)
    return len(b), regs


# =========================
# LoRa config apply
# =========================
def apply_lora_config_to_all_nodes(client, cfg: Dict[str, Any]) -> bool:
    freq_x10 = int(round(float(cfg["freq_mhz"]) * 10.0))
    regs = [
        freq_x10,
        int(cfg["tx_power"]),
        int(cfg["sf"]),
        int(cfg["bw_khz"]),
        int(cfg["cr_denom"]),
    ]

    ok_any = False
    for sid in NODES:
        try:
            w = _call(client.write_registers, LORA_BASE, regs, device_id=sid)
            if w is None or w.isError():
                print(f"[Node {sid}] ❌ write 400..404 failed")
                continue

            a = _call(client.write_register, LORA_APPLY, 1, device_id=sid)
            if a is None or a.isError():
                print(f"[Node {sid}] ❌ apply 410 failed")
                continue

            ok_any = True
            print(f"[Node {sid}] ✅ LoRa set")
            time.sleep(0.1)
        except Exception as e:
            print(f"[Node {sid}] ❌ exception: {e}")

    time.sleep(1.0)
    return ok_any


# =========================
# Modbus read/write helpers
# =========================
def read_rx_block(client, slave_id: int) -> Optional[Dict[str, Any]]:
    try:
        res = _call(client.read_holding_registers, address=RX_BLOCK_ADDR, count=RX_BLOCK_COUNT, device_id=slave_id)
        if res is None or res.isError():
            return None

        regs = res.registers
        rx_id = regs[0]
        node_ts = ((regs[12] << 16) | regs[13]) & 0xFFFFFFFF
        rssi = to_int16(regs[14]) / 10.0
        snr = to_int16(regs[15]) / 10.0

        return {"rx_id": rx_id, "node_timestamp": node_ts, "rssi": rssi, "snr": snr}

    except (ModbusIOException, ModbusException):
        return None
    except Exception:
        return None


def rx_block_stable(client, slave_id: int, stable_reads: int = RX_STABLE_READS) -> Optional[Dict[str, Any]]:
    last = None
    same = 0
    for _ in range(10):
        cur = read_rx_block(client, slave_id)
        if cur is None:
            time.sleep(RX_STABLE_GAP_S)
            continue

        if last is not None and cur["rx_id"] == last["rx_id"] and cur["node_timestamp"] == last["node_timestamp"]:
            same += 1
            if same >= (stable_reads - 1):
                return cur
        else:
            same = 0
            last = cur

        time.sleep(RX_STABLE_GAP_S)

    return last


def read_bme_block(client, slave_id: int) -> Optional[Dict[str, Any]]:
    try:
        res = _call(client.read_holding_registers, address=BME_TEMP_REG, count=3, device_id=slave_id)
        if res is None or res.isError():
            return None

        regs = res.registers
        temp = to_int16(regs[0]) / 100.0
        hum = regs[1] / 100.0
        press = regs[2] / 10.0

        return {"bme_temp": temp, "bme_hum": hum, "bme_press": press}
    except Exception:
        return None


def wait_for_bme_ready(client, rid: int) -> Tuple[Optional[Dict[str, Any]], float]:
    t0 = time.perf_counter()
    deadline = time.time() + BME_TIMEOUT_S
    last = None

    while time.time() < deadline:
        bme = read_bme_block(client, rid)
        last = bme

        if bme is None:
            time.sleep(BME_POLL_S)
            continue

        if not REQUIRE_BME:
            return bme, (time.perf_counter() - t0) * 1000.0

        if not (bme["bme_temp"] == 0.0 and bme["bme_hum"] == 0.0 and bme["bme_press"] == 0.0):
            return bme, (time.perf_counter() - t0) * 1000.0

        time.sleep(BME_POLL_S)

    return last, (time.perf_counter() - t0) * 1000.0


def collect_round_bme(client) -> Dict[int, Dict[str, Any]]:
    """
    Reads BME only once per round for nodes 5..8.
    The values are later only entered on the rows with matching receiver_id.
    """
    out: Dict[int, Dict[str, Any]] = {}

    for sid in BME_NODES:
        try:
            send_bme_cmd(client, sid)
            bme, wait_ms = wait_for_bme_ready(client, sid)
            if bme is not None:
                out[sid] = {
                    "bme_temp": bme.get("bme_temp"),
                    "bme_hum": bme.get("bme_hum"),
                    "bme_press": bme.get("bme_press"),
                    "bme_wait_ms": wait_ms,
                }
        except Exception as e:
            log(f"[Node {sid}] BME round-read failed: {e}")

    return out


def write_payload(client, sid: int, text: str) -> Tuple[int, int]:
    plen, regs = pack_ascii_to_regs(text)

    w1 = _call(client.write_register, PAYLOAD_LEN_REG, plen, device_id=sid)
    if w1 is None or w1.isError():
        raise RuntimeError(f"[Node {sid}] payload_len write failed")

    if regs:
        w2 = _call(client.write_registers, PAYLOAD_DATA_BASE, regs, device_id=sid)
        if w2 is None or w2.isError():
            raise RuntimeError(f"[Node {sid}] payload_data write failed")

    return plen, len(regs)


def write_tx_timestamp(client, sid: int, ts32: int) -> None:
    hi = (ts32 >> 16) & 0xFFFF
    lo = ts32 & 0xFFFF
    w = _call(client.write_registers, TX_TIMESTAMP_BASE_REG, [hi, lo], device_id=sid)
    if w is None or w.isError():
        raise RuntimeError(f"[Node {sid}] tx_timestamp write failed")


def send_tx_cmd(client, sid: int) -> None:
    w = _call(client.write_register, CMD_REGISTER, CMD_TX, device_id=sid)
    if w is None or w.isError():
        raise RuntimeError(f"[Node {sid}] TX command failed")


def send_bme_cmd(client, sid: int) -> None:
    w = _call(client.write_register, CMD_REGISTER, CMD_BME, device_id=sid)
    if w is None or w.isError():
        raise RuntimeError(f"[Node {sid}] BME command failed")


def send_led_cmd(client, sid: int, on: bool) -> None:
    cmd = CMD_LED_ON if on else CMD_LED_OFF
    w = _call(client.write_register, CMD_REGISTER, cmd, device_id=sid)
    if w is None or w.isError():
        raise RuntimeError(f"[Node {sid}] LED command failed")


# =========================
# Measurement loop
# =========================
def one_round(client, conn: sqlite3.Connection, round_id: int, lora_cfg: Dict[str, Any],
               csv_path: Optional[Path] = None) -> Tuple[int, int]:
    """Runs one round (every node sends once to every other node) and
    stores measurement + rounds (SQLite) as well as optionally a CSV row
    per link. Returns (total_links, ok_links)."""
    cur = conn.cursor()

    pc_start = now_iso()
    t_round0 = time.perf_counter()

    nodes_csv = ",".join(str(n) for n in NODES)

    total_links = 0
    ok_links = 0
    timeouts = 0

    if VERBOSE:
        log("")
        log(f"========== ROUND {round_id} START "
            f"(freq={lora_cfg['freq_mhz']:.1f} MHz, power={lora_cfg['tx_power']} dBm, "
            f"bw={lora_cfg['bw_khz']} kHz) ==========")

    payload_len = None
    for sid in NODES:
        try:
            plen, _ = write_payload(client, sid, PAYLOAD_TEXT)
            payload_len = plen
        except Exception as e:
            log(f"[Node {sid}] payload write failed: {e}")

    # Read BME only once per round
    round_bme = collect_round_bme(client)

    # Turn off all LEDs as a precaution
    for sid in NODES:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    for sender_id in NODES:
        tx_timestamp = make_tx_timestamp_31()

        # Only the current sender's LED lights up
        for sid in NODES:
            if sid == sender_id:
                continue
            try:
                send_led_cmd(client, sid, False)
            except Exception:
                pass

        try:
            send_led_cmd(client, sender_id, True)
        except Exception as e:
            log(f"[Node {sender_id}] LED ON failed: {e}")

        try:
            write_tx_timestamp(client, sender_id, tx_timestamp)

            t_send0 = time.perf_counter()
            send_tx_cmd(client, sender_id)
            t_send_done = time.perf_counter()

            trigger_ms = (t_send_done - t_send0) * 1000.0
        except Exception as e:
            log(f"[TX Node {sender_id}] TX failed: {e}")
            try:
                send_led_cmd(client, sender_id, False)
            except Exception:
                pass
            continue

        for receiver_id in NODES:
            if receiver_id == sender_id:
                continue

            total_links += 1
            deadline = time.time() + RX_TIMEOUT_S
            rx = None

            while time.time() < deadline:
                rx = rx_block_stable(client, receiver_id)
                if rx is None:
                    time.sleep(RX_POLL_S)
                    continue

                id_match_now = int(rx.get("rx_id", -1)) == int(sender_id)
                ts_match_now = int(rx.get("node_timestamp", -1)) == int(tx_timestamp)

                if id_match_now and ts_match_now:
                    break

                time.sleep(RX_POLL_S)

            t_match_or_timeout = time.perf_counter()

            rx_wait_ms = (t_match_or_timeout - t_send_done) * 1000.0
            e2e_ms = (t_match_or_timeout - t_send0) * 1000.0

            id_match = (rx is not None and int(rx.get("rx_id", -1)) == int(sender_id))
            ts_match = (rx is not None and int(rx.get("node_timestamp", -1)) == int(tx_timestamp))
            ok_match = 1 if (id_match and ts_match) else 0

            if ok_match:
                ok_links += 1
            else:
                timeouts += 1

            # Fetch BME only from the round cache, and only matching the receiver_id
            bme_entry = round_bme.get(receiver_id)
            bme_wait_ms = bme_entry.get("bme_wait_ms") if bme_entry else None
            bme_temp = bme_entry.get("bme_temp") if bme_entry else None
            bme_hum = bme_entry.get("bme_hum") if bme_entry else None
            bme_press = bme_entry.get("bme_press") if bme_entry else None

            if VERBOSE and PRINT_LINK_LINE:
                rssi_txt = f"{rx.get('rssi'):.1f}" if (rx is not None and rx.get("rssi") is not None) else "None"
                snr_txt = f"{rx.get('snr'):.1f}" if (rx is not None and rx.get("snr") is not None) else "None"

                bt_txt = f"{bme_temp:.2f}" if bme_temp is not None else "None"
                bh_txt = f"{bme_hum:.2f}" if bme_hum is not None else "None"
                bp_txt = f"{bme_press:.1f}" if bme_press is not None else "None"

                status = "OK" if ok_match else "TIMEOUT"

                log(
                    f"{status} {sender_id}->{receiver_id} | "
                    f"RSSI={rssi_txt} dBm | "
                    f"SNR={snr_txt} dB | "
                    f"rx_wait={rx_wait_ms:.1f} ms | "
                    f"e2e={e2e_ms:.1f} ms | "
                    f"T={bt_txt} °C H={bh_txt} % P={bp_txt} hPa"
                )

            row = {
                "pc_ts": now_iso(),
                "round_id": round_id,
                "sender_id": sender_id,
                "receiver_id": receiver_id,

                # marker field is kept, but now contains the sent TX timestamp
                "marker": tx_timestamp,
                "payload_len": payload_len,
                "payload_text": PAYLOAD_TEXT,

                "trigger_ms": trigger_ms,
                "rx_wait_ms": rx_wait_ms,
                "e2e_ms": e2e_ms,

                "ok_match": ok_match,

                "rx_id": (rx.get("rx_id") if rx else None),
                "node_timestamp": (rx.get("node_timestamp") if rx else None),

                "rssi": (rx.get("rssi") if rx else None),
                "snr": (rx.get("snr") if rx else None),

                "bme_wait_ms": bme_wait_ms,
                "bme_temp": bme_temp,
                "bme_hum": bme_hum,
                "bme_press": bme_press,
            }

            try:
                store_measurement(cur, row)
            except Exception as e:
                log(f"[DB] store_measurement failed: {e}")

            if csv_path is not None:
                csv_row = {
                    "pc_ts": row["pc_ts"],
                    "round_id": row["round_id"],
                    "sender_id": row["sender_id"],
                    "receiver_id": row["receiver_id"],
                    "freq_mhz": lora_cfg.get("freq_mhz"),
                    "tx_power": lora_cfg.get("tx_power"),
                    "sf": lora_cfg.get("sf"),
                    "bw_khz": lora_cfg.get("bw_khz"),
                    "cr_denom": lora_cfg.get("cr_denom"),
                    "payload_len": row["payload_len"],
                    "trigger_ms": row["trigger_ms"],
                    "rx_wait_ms": row["rx_wait_ms"],
                    "e2e_ms": row["e2e_ms"],
                    "ok_match": row["ok_match"],
                    "rx_id": row["rx_id"],
                    "node_timestamp": row["node_timestamp"],
                    "rssi": row["rssi"],
                    "snr": row["snr"],
                    "bme_temp": row["bme_temp"],
                    "bme_hum": row["bme_hum"],
                    "bme_press": row["bme_press"],
                }
                try:
                    append_csv_row(csv_path, MEASUREMENT_CSV_FIELDS, csv_row)
                except Exception as e:
                    log(f"[CSV] append failed: {e}")

        # Turn sender LED back off
        try:
            send_led_cmd(client, sender_id, False)
        except Exception as e:
            log(f"[Node {sender_id}] LED OFF failed: {e}")

    # Turn off all LEDs at the end of the round as a precaution
    for sid in NODES:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    pc_end = now_iso()
    duration_s = float(time.perf_counter() - t_round0)

    round_info = {
        "round_id": round_id,
        "pc_start": pc_start,
        "pc_end": pc_end,
        "duration_s": duration_s,

        "nodes_csv": nodes_csv,
        "payload_len": payload_len,
        "payload_text": PAYLOAD_TEXT,

        "ok_links": ok_links,
        "total_links": total_links,
        "timeouts": timeouts,

        "freq_mhz": lora_cfg.get("freq_mhz"),
        "tx_power": lora_cfg.get("tx_power"),
        "sf": lora_cfg.get("sf"),
        "bw_khz": lora_cfg.get("bw_khz"),
        "cr_denom": lora_cfg.get("cr_denom"),
        "lora_applied": lora_cfg.get("lora_applied", 0),

        "port": PORT,
        "baudrate": BAUDRATE,
        "measure_interval_s": MEASURE_INTERVAL_S,
        "notes": lora_cfg.get("notes", ""),
    }

    try:
        store_round(cur, round_info)
        safe_commit(conn)
    except Exception as e:
        log(f"[DB] store_round/commit failed: {e}")

    log(f"[Round {round_id}] ok={ok_links}/{total_links} timeouts={timeouts} dur={duration_s:.1f}s")

    return total_links, ok_links


# =========================
# Sweep control
# =========================
def ensure_connected(client) -> None:
    """pymodbus closes the port after retries+3 missed replies and does not
    reopen it by itself, so reconnect before each block of requests."""
    if not client.connected:
        log("[Modbus] connection closed, reconnecting...")
        client.connect()


def preflight_check(client) -> bool:
    """Reads the LoRa config registers from every node once. Returns True if at
    least one node answers."""
    ok_any = False
    for sid in NODES:
        try:
            res = _call(client.read_holding_registers, address=LORA_BASE, count=5, device_id=sid)
            if res is not None and not res.isError():
                log(f"[Node {sid}] reachable, LoRa regs 400..404 = {res.registers}")
                ok_any = True
            else:
                log(f"[Node {sid}] no valid response")
        except Exception as e:
            log(f"[Node {sid}] no response: {e}")
        ensure_connected(client)
    return ok_any



def run_sweep(client, conn: sqlite3.Connection, next_round: int) -> None:
    """Interleaves all SWEEP_CONFIGS: each pass runs one round per config
    (config 1, config 2, ...), repeated for RUN_MAX_ROUNDS passes. A round
    starts every MEASURE_INTERVAL_S (config switch included). round_id stays
    continuous across all rounds. The manifest (one row per config) is
    written at the end, also when stopped early with Ctrl+C."""
    stats = []
    for config_id, cfg in enumerate(SWEEP_CONFIGS, start=1):
        csv_path = config_csv_path(cfg)
        init_csv(csv_path, MEASUREMENT_CSV_FIELDS)
        stats.append({
            "config_id": config_id, "cfg": cfg, "csv_path": csv_path,
            "start_ts": None, "end_ts": None, "duration_s": 0.0,
            "rounds_completed": 0, "total_links": 0, "ok_links": 0,
        })

    round_id = next_round
    n_cfg = len(SWEEP_CONFIGS)
    total_rounds = RUN_MAX_ROUNDS * n_cfg
    done_rounds = 0

    try:
        for pass_idx in range(1, RUN_MAX_ROUNDS + 1):
            for st in stats:
                config_id, cfg = st["config_id"], st["cfg"]
                t_round = time.perf_counter()
                ensure_connected(client)

                log(f"\n#### Pass {pass_idx}/{RUN_MAX_ROUNDS} | Configuration {config_id}/{n_cfg}: "
                    f"freq={cfg['freq_mhz']:.1f} MHz, power={cfg['tx_power']} dBm, "
                    f"SF{cfg['sf']}, bw={cfg['bw_khz']} kHz, CR4/{cfg['cr_denom']} ####")

                ok = apply_lora_config_to_all_nodes(client, cfg)
                lora_cfg = {**cfg, "lora_applied": 1 if ok else 0, "notes": f"sweep_config_id={config_id}"}
                time.sleep(CONFIG_SETTLE_S)

                if st["start_ts"] is None:
                    st["start_ts"] = now_iso()
                total_links, ok_links = one_round(client, conn, round_id, lora_cfg, csv_path=st["csv_path"])
                st["end_ts"] = now_iso()
                st["total_links"] += total_links
                st["ok_links"] += ok_links
                st["rounds_completed"] += 1
                round_id += 1
                done_rounds += 1

                dt = time.perf_counter() - t_round
                st["duration_s"] += dt

                # No wait after the very last round of the sweep
                if done_rounds >= total_rounds:
                    break

                sleep_s = max(0.0, float(MEASURE_INTERVAL_S) - dt)
                if sleep_s > 0:
                    log(f"[Round {round_id - 1}] next round in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                else:
                    log(f"[Round {round_id - 1}] WARNING: round took {dt:.1f}s > interval "
                        f"{MEASURE_INTERVAL_S:.0f}s, starting next round immediately")
    finally:
        init_csv(MANIFEST_CSV_PATH, MANIFEST_CSV_FIELDS)
        for st in stats:
            if st["rounds_completed"] == 0:
                continue
            cfg = st["cfg"]
            append_csv_row(MANIFEST_CSV_PATH, MANIFEST_CSV_FIELDS, {
                "config_id": st["config_id"],
                "freq_mhz": cfg["freq_mhz"],
                "tx_power": cfg["tx_power"],
                "sf": cfg["sf"],
                "bw_khz": cfg["bw_khz"],
                "cr_denom": cfg["cr_denom"],
                "csv_file": st["csv_path"].name,
                "start_ts": st["start_ts"],
                "end_ts": st["end_ts"],
                "duration_s": round(st["duration_s"], 1),
                "rounds_completed": st["rounds_completed"],
                "total_links": st["total_links"],
                "ok_links": st["ok_links"],
            })
            log(f"#### Configuration {st['config_id']}: {st['rounds_completed']} rounds, "
                f"{st['ok_links']}/{st['total_links']} links ok -> {st['csv_path'].name} ####")


def main():
    print("=== lora_logger_sweep: automated LoRa configuration sweep ===")
    print(f"DB: {DB_PATH}")
    print(f"CSV: {CSV_OUT_DIR.resolve()}")
    print(f"PORT: {PORT} @ {BAUDRATE}")
    print(f"NODES: {NODES}")
    print(f"Configs: {len(SWEEP_CONFIGS)} (interleaved) | per config: {RUN_MAX_ROUNDS} rounds, "
          f"interval {MEASURE_INTERVAL_S:.0f}s")

    init_db(DB_PATH)

    client = ModbusSerialClient(
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity='N',
        timeout=1.0, retries=2,
    )
    if not client.connect():
        raise RuntimeError("Modbus connect failed")

    if not preflight_check(client):
        client.close()
        raise RuntimeError(
            f"No node answered on {PORT} @ {BAUDRATE}. Check: correct port (ls /dev/ttyUSB*), "
            f"no other process using it (fuser {PORT}), RS485 adapter wiring/power, node IDs."
        )

    conn = connect_db(DB_PATH, timeout_s=SQLITE_CONNECT_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};")

    cur = conn.cursor()
    try:
        row = cur.execute("SELECT MAX(round_id) FROM rounds;").fetchone()
        next_round = int(row[0] or 0) + 1
    except Exception:
        next_round = 1

    print("\n=== Start sweep ===")
    print("Stop early with Ctrl+C (rounds so far are kept, manifest is still written)\n")

    try:
        run_sweep(client, conn, next_round)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    print(f"\nDone. Results in {DB_PATH} (tables: '{MEASUREMENT_TABLE}', '{ROUNDS_TABLE}') "
          f"and per-config CSVs in {CSV_OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
