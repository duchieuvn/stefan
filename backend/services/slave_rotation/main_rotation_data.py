"""Rotation logger for firmware/slave_rotation: alternates LoRa rounds and CW rounds.

One image on every node does both. The master decides which one is active:

  LoRa round: every node sends one packet (CMD 10), all others report RSSI/SNR (regs 100..115).
  CW round:   every node sends one carrier (CMD 30), all others measure RSSI (CMD 32, regs 130..135).

Before every round the service writes the wanted mode to register 420 of all nodes and waits
until register 421 reports it. A node that does not follow (rebooted, radio error, busy) is
left out of that round, so a round never mixes modes; with fewer than two nodes in the wanted
mode the round is skipped.

LoRa rounds are stored like main_senor_data.py does (setting, rounds, measurement), CW rounds
like main_cw_data.py does (cw_rounds, cw_measurement) - same tables, no schema change.

Self-contained: nothing is imported from services/sensor_logger. A service for manage_services.py
would only need run(ctx, lora="read"); started as a script it runs standalone (Ctrl+C stops it).
"""
from __future__ import annotations

from pymodbus.client import ModbusSerialClient
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config import *

# =========================
# Register / command map (firmware/slave_rotation/modbus_handler.h)
# =========================
REG_CMD = 200
CMD_LED_ON = 1
CMD_LED_OFF = 2
CMD_TX = 10                 # LoRa packet (LoRa mode)
CMD_CW_START = 30           # CW carrier (CW mode)
CMD_CW_STOP = 31
CMD_RSSI_MEASURE = 32

REG_TX_TIMESTAMP = 300      # 300/301 = 31-bit timestamp of the packet
REG_PAYLOAD_LEN = 320
REG_PAYLOAD_DATA = 321
PAYLOAD_MAX_BYTES = 64

RX_BLOCK_ADDR = 100         # 100..115: rx id, ... 112/113 timestamp, 114 RSSI, 115 SNR
RX_BLOCK_COUNT = 16

REG_RSSI_BLOCK = 130        # 130 = RSSI avg x10 ... 135 = measurement counter
RSSI_BLOCK_COUNT = 6
REG_CW_TX_MS = 360          # 360 = carrier duration ms, 361 = measure duration ms

REG_RADIO = 400             # 400 = freq x10, 401 = power dBm, 402 = SF, 403 = BW kHz, 404 = CR
REG_RADIO_APPLY = 410

REG_MODE_REQ = 420          # 0 = LoRa, 1 = CW
REG_MODE_ACTIVE = 421       # 0 = LoRa, 1 = CW, 0xFFFF = radio init failed
MODE_LORA = 0
MODE_CW = 1
MODE_NAMES = {MODE_LORA: "LoRa", MODE_CW: "CW"}

# Träger muss laufen, bis der letzte Empfänger fertig gemessen hat:
# Messdauer + Zeit für die CMD-32-Writes an alle Empfänger + Reserve.
# Wird nach den Messungen mit CMD 31 ohnehin sofort gestoppt.
CW_START_GAP_MS = 150


def cw_tx_ms(n_nodes: int) -> int:
    return min(CW_TX_MAX_MS, CW_MEASURE_MS + CW_START_GAP_MS * n_nodes + 500)


# =========================
# SQLite
# =========================
MEASUREMENT_TABLE = "measurement"
ROUNDS_TABLE = "rounds"
SETTING_TABLE = "setting"
CW_MEASUREMENT_TABLE = "cw_measurement"
CW_ROUNDS_TABLE = "cw_rounds"

SETTING_COLUMNS = (
    "freq_mhz", "tx_power", "sf", "bw_khz", "cr_denom", "lora_applied",
    "port", "baudrate", "measure_interval_s",
)


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

    # LoRa rounds
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(MEASUREMENT_TABLE)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc_ts TEXT NOT NULL,

            round_id INTEGER,

            sender_id INTEGER,
            receiver_id INTEGER,

            ok_match INTEGER,

            node_timestamp INTEGER,

            rssi REAL,
            snr REAL
        );
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(SETTING_TABLE)} (
            setting_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,

            freq_mhz REAL,
            tx_power INTEGER,
            sf INTEGER,
            bw_khz INTEGER,
            cr_denom INTEGER,
            lora_applied INTEGER,

            port TEXT,
            baudrate INTEGER,
            measure_interval_s REAL
        );
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(ROUNDS_TABLE)} (
            round_id INTEGER PRIMARY KEY,

            pc_start TEXT,
            pc_end TEXT,
            duration_s REAL,

            nodes_csv TEXT,

            ok_links INTEGER,
            total_links INTEGER,
            timeouts INTEGER,

            setting_id INTEGER REFERENCES {quote_ident(SETTING_TABLE)}(setting_id)
        );
    """)

    # CW rounds
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(CW_MEASUREMENT_TABLE)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc_ts TEXT NOT NULL,
            round_id INTEGER,
            sender_id INTEGER,
            receiver_id INTEGER,
            rssi REAL              -- dBm, NULL = keine Messung erhalten
        );
    """)

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(CW_ROUNDS_TABLE)} (
            round_id INTEGER PRIMARY KEY,
            pc_start TEXT,
            pc_end TEXT,
            duration_s REAL,
            nodes_csv TEXT,
            ok_links INTEGER,
            total_links INTEGER,
            freq_mhz REAL,
            tx_power INTEGER
        );
    """)

    con.commit()
    con.close()


def store_measurement(cur: sqlite3.Cursor, row: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT INTO {quote_ident(MEASUREMENT_TABLE)} (
            pc_ts,
            round_id, sender_id, receiver_id,
            ok_match, node_timestamp,
            rssi, snr
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        row.get("pc_ts"),
        row.get("round_id"), row.get("sender_id"), row.get("receiver_id"),
        row.get("ok_match"), row.get("node_timestamp"),
        row.get("rssi"), row.get("snr"),
    ))


def get_or_create_setting(cur: sqlite3.Cursor, values: Dict[str, Any]) -> int:
    """Return the id of the latest setting row equal to `values`, or insert a new one."""
    params = tuple(values.get(c) for c in SETTING_COLUMNS)
    match = " AND ".join(f"{c} IS ?" for c in SETTING_COLUMNS)
    row = cur.execute(
        f"SELECT setting_id FROM {quote_ident(SETTING_TABLE)} WHERE {match} "
        "ORDER BY setting_id DESC LIMIT 1;",
        params,
    ).fetchone()
    if row:
        return int(row[0])

    cur.execute(
        f"INSERT INTO {quote_ident(SETTING_TABLE)} (created_at, {', '.join(SETTING_COLUMNS)}) "
        f"VALUES (?, {', '.join('?' * len(SETTING_COLUMNS))});",
        (now_iso(), *params),
    )
    return int(cur.lastrowid)


def store_round(cur: sqlite3.Cursor, info: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT OR REPLACE INTO {quote_ident(ROUNDS_TABLE)} (
            round_id,
            pc_start, pc_end, duration_s,

            nodes_csv,

            ok_links, total_links, timeouts,

            setting_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        info.get("round_id"),
        info.get("pc_start"), info.get("pc_end"), info.get("duration_s"),

        info.get("nodes_csv"),

        info.get("ok_links"), info.get("total_links"), info.get("timeouts"),

        info.get("setting_id"),
    ))


def store_cw_measurement(cur: sqlite3.Cursor, row: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT INTO {quote_ident(CW_MEASUREMENT_TABLE)} (
            pc_ts, round_id, sender_id, receiver_id, rssi
        ) VALUES (?, ?, ?, ?, ?);
    """, (
        row.get("pc_ts"), row.get("round_id"),
        row.get("sender_id"), row.get("receiver_id"), row.get("rssi"),
    ))


def store_cw_round(cur: sqlite3.Cursor, info: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT OR REPLACE INTO {quote_ident(CW_ROUNDS_TABLE)} (
            round_id, pc_start, pc_end, duration_s, nodes_csv,
            ok_links, total_links, freq_mhz, tx_power
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        info.get("round_id"), info.get("pc_start"), info.get("pc_end"), info.get("duration_s"),
        info.get("nodes_csv"), info.get("ok_links"), info.get("total_links"),
        info.get("freq_mhz"), info.get("tx_power"),
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
# Generic helpers
# =========================
class StandaloneContext:
    """Stands in for the manager's ServiceContext when this file is run as a script."""

    def stopping(self) -> bool:
        return False

    def wait(self, seconds: float) -> bool:
        time.sleep(max(0.0, seconds))
        return False

    def report(self, **info: object) -> None:
        pass


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


def write_reg(client, sid: int, addr: int, value: int) -> bool:
    try:
        w = _call(client.write_register, addr, value, device_id=sid)
        return w is not None and not w.isError()
    except Exception:
        return False


def write_regs(client, sid: int, addr: int, values: List[int]) -> bool:
    try:
        w = _call(client.write_registers, addr, values, device_id=sid)
        return w is not None and not w.isError()
    except Exception:
        return False


def read_regs(client, sid: int, addr: int, count: int) -> Optional[List[int]]:
    try:
        res = _call(client.read_holding_registers, address=addr, count=count, device_id=sid)
        if res is None or res.isError() or len(res.registers) < count:
            return None
        return list(res.registers)
    except Exception:
        return None


def ask_int(prompt: str, default: int, min_v: int, max_v: int) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if min_v <= value <= max_v:
            return value
        print(f"Please enter a value between {min_v} and {max_v}.")


def ask_float(prompt: str, default: float, min_v: float, max_v: float) -> float:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            print("Please enter a number.")
            continue
        if min_v <= value <= max_v:
            return value
        print(f"Please enter a value between {min_v} and {max_v}.")


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        raw = input(f"{prompt} {suffix}: ").strip().lower()
        if not raw:
            return default_yes
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# =========================
# Radio settings (registers 400..404, used in both modes)
# =========================
def read_radio_config(client, sid: int) -> Optional[Dict[str, Any]]:
    regs = read_regs(client, sid, REG_RADIO, 5)
    if regs is None:
        return None
    return {
        "freq_mhz": regs[0] / 10.0,
        "tx_power": to_int16(regs[1]),
        "sf": regs[2],
        "bw_khz": regs[3],
        "cr_denom": regs[4],
        "lora_applied": 1,
    }


def radio_summary(cfg: Dict[str, Any]) -> str:
    return (
        f"F={cfg['freq_mhz']:.1f} MHz, P={cfg['tx_power']}, "
        f"SF{cfg['sf']}, BW={cfg['bw_khz']}, CR=4/{cfg['cr_denom']}"
    )


def read_radio_config_or_defaults(client) -> Dict[str, Any]:
    """Without questions: don't rewrite the nodes' settings, read them from the first node."""
    print("\n=== Radio configuration ===")
    print(f"-> Reading the current settings of node {NODES[0]} ...")
    cfg = read_radio_config(client, NODES[0])
    if cfg is not None:
        print(f"-> Read: {radio_summary(cfg)}")
        return cfg

    cfg = {
        "freq_mhz": DEFAULT_FREQ_MHZ,
        "tx_power": DEFAULT_POWER,
        "sf": DEFAULT_SF,
        "bw_khz": DEFAULT_BW,
        "cr_denom": DEFAULT_CR,
        "lora_applied": 0,
    }
    print(f"-> Read failed. Using the defaults: {radio_summary(cfg)}")
    return cfg


def apply_radio_config_to_all_nodes(client, cfg: Dict[str, Any]) -> bool:
    regs = [
        int(round(float(cfg["freq_mhz"]) * 10.0)),
        int(cfg["tx_power"]) & 0xFFFF,
        int(cfg["sf"]),
        int(cfg["bw_khz"]),
        int(cfg["cr_denom"]),
    ]
    ok_any = False
    for sid in NODES:
        if not write_regs(client, sid, REG_RADIO, regs):
            print(f"[Node {sid}] ❌ write {REG_RADIO}..{REG_RADIO + 4} failed")
            continue
        if not write_reg(client, sid, REG_RADIO_APPLY, 1):
            print(f"[Node {sid}] ❌ apply {REG_RADIO_APPLY} failed")
            continue
        ok_any = True
        print(f"[Node {sid}] ✅ radio settings written")
        time.sleep(0.1)
    time.sleep(1.0)
    return ok_any


def configure_radio_interactive(client) -> Dict[str, Any]:
    """Write new settings to all nodes, or keep what the nodes have."""
    print("\n=== Radio configuration ===")
    current = read_radio_config_or_defaults(client)

    if not ask_yes_no("Write new radio settings to all nodes now?", default_yes=False):
        return current

    cfg = {
        "freq_mhz": ask_float("Frequency MHz (LoRa and CW)", current["freq_mhz"], 100.0, 1000.0),
        "tx_power": ask_int("Power dBm (LoRa and CW)", current["tx_power"], -9, 22),
        "sf": ask_int("Spreading factor (LoRa only)", current["sf"], 7, 12),
        "bw_khz": ask_int("Bandwidth kHz (LoRa only)", current["bw_khz"], 7, 500),
        "cr_denom": ask_int("Coding rate 4/x (LoRa only)", current["cr_denom"], 5, 8),
        "lora_applied": 0,
    }
    print(f"\n-> Writing to all nodes: {radio_summary(cfg)}")
    cfg["lora_applied"] = 1 if apply_radio_config_to_all_nodes(client, cfg) else 0
    return cfg


# =========================
# Mode synchronisation
# =========================
def read_active_mode(client, sid: int) -> Optional[int]:
    regs = read_regs(client, sid, REG_MODE_ACTIVE, 1)
    return None if regs is None else regs[0]


def sync_nodes(client, mode: int, ctx) -> List[int]:
    """
    Bring all nodes into `mode` (register 420, confirmed by 421) and return the ones that
    are there. Nodes that don't follow within the retries are left out of the round.
    """
    name = MODE_NAMES[mode]
    pending = list(NODES)
    switched = False

    for attempt in range(1, MODE_SWITCH_RETRIES + 1):
        pending = [sid for sid in pending if read_active_mode(client, sid) != mode]
        if not pending or ctx.stopping():
            break

        for sid in pending:
            if write_reg(client, sid, REG_MODE_REQ, mode):
                switched = True
            else:
                log(f"[Node {sid}] mode request ({name}) not written")

        deadline = time.time() + MODE_SWITCH_TIMEOUT_S
        while pending and time.time() < deadline and not ctx.stopping():
            time.sleep(MODE_POLL_S)
            pending = [sid for sid in pending if read_active_mode(client, sid) != mode]

        if not pending or ctx.stopping():
            break
        log(f"[Mode] attempt {attempt}/{MODE_SWITCH_RETRIES}: node(s) {pending} not in {name} mode yet")

    ready = [sid for sid in NODES if sid not in pending]
    if pending and not ctx.stopping():
        log(f"[Mode] node(s) {pending} left out of this {name} round")
    if switched and ready:
        time.sleep(MODE_SETTLE_S)   # let the radios settle after re-init
    return ready


def stop_all(client) -> None:
    """No carrier may keep running after the service ends; leave the nodes in their boot mode."""
    for sid in NODES:
        write_reg(client, sid, REG_CMD, CMD_CW_STOP)
    for sid in NODES:
        write_reg(client, sid, REG_MODE_REQ, MODE_LORA)


# =========================
# LoRa round (same flow as main_senor_data.py)
# =========================
def read_rx_block(client, slave_id: int) -> Optional[Dict[str, Any]]:
    regs = read_regs(client, slave_id, RX_BLOCK_ADDR, RX_BLOCK_COUNT)
    if regs is None:
        return None
    return {
        "rx_id": regs[0],
        "node_timestamp": ((regs[12] << 16) | regs[13]) & 0xFFFFFFFF,
        "rssi": to_int16(regs[14]) / 10.0,
        "snr": to_int16(regs[15]) / 10.0,
    }


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


def write_payload(client, sid: int, text: str) -> None:
    plen, regs = pack_ascii_to_regs(text)

    if not write_reg(client, sid, REG_PAYLOAD_LEN, plen):
        raise RuntimeError(f"[Node {sid}] payload_len write failed")
    if regs and not write_regs(client, sid, REG_PAYLOAD_DATA, regs):
        raise RuntimeError(f"[Node {sid}] payload_data write failed")


def send_led_cmd(client, sid: int, on: bool) -> None:
    if not write_reg(client, sid, REG_CMD, CMD_LED_ON if on else CMD_LED_OFF):
        raise RuntimeError(f"[Node {sid}] LED command failed")


def lora_round(client, conn: sqlite3.Connection, round_id: int, setting_id: int,
               nodes: List[int], ctx) -> Optional[Dict[str, Any]]:
    """
    One LoRa round among `nodes`; returns the stored round data, or None if ctx stops
    in the middle (the round is then discarded).
    """
    cur = conn.cursor()

    pc_start = now_iso()
    t_round0 = time.perf_counter()

    total_links = 0
    ok_links = 0
    timeouts = 0

    if VERBOSE:
        log("")
        log(f"========== LoRa ROUND {round_id} START ==========")

    for sid in nodes:
        try:
            write_payload(client, sid, PAYLOAD_TEXT)
        except Exception as e:
            log(f"[Node {sid}] payload write failed: {e}")

    # Sicherheitshalber alle LEDs aus
    for sid in nodes:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    aborted = False
    for sender_id in nodes:
        if ctx.stopping():
            aborted = True
            break

        tx_timestamp = make_tx_timestamp_31()

        # Nur aktueller Sender leuchtet
        for sid in nodes:
            if sid == sender_id:
                continue
            try:
                send_led_cmd(client, sid, False)
            except Exception:
                pass

        try:
            send_led_cmd(client, sender_id, True)
        except Exception as e:
            log(f"[Node {sender_id}] LED AN failed: {e}")

        if not write_regs(client, sender_id, REG_TX_TIMESTAMP, [(tx_timestamp >> 16) & 0xFFFF, tx_timestamp & 0xFFFF]) \
                or not write_reg(client, sender_id, REG_CMD, CMD_TX):
            log(f"[TX Node {sender_id}] TX failed")
            try:
                send_led_cmd(client, sender_id, False)
            except Exception:
                pass
            continue

        for receiver_id in nodes:
            if receiver_id == sender_id:
                continue

            total_links += 1
            deadline = time.time() + RX_TIMEOUT_S
            rx = None

            while time.time() < deadline and not ctx.stopping():
                rx = rx_block_stable(client, receiver_id)
                if rx is None:
                    time.sleep(RX_POLL_S)
                    continue

                if int(rx["rx_id"]) == int(sender_id) and int(rx["node_timestamp"]) == int(tx_timestamp):
                    break

                time.sleep(RX_POLL_S)

            if ctx.stopping():
                aborted = True
                break

            ok_match = int(rx is not None
                           and int(rx["rx_id"]) == int(sender_id)
                           and int(rx["node_timestamp"]) == int(tx_timestamp))
            if ok_match:
                ok_links += 1
            else:
                timeouts += 1

            if VERBOSE and PRINT_LINK_LINE:
                rssi_txt = f"{rx['rssi']:.1f}" if rx is not None else "None"
                snr_txt = f"{rx['snr']:.1f}" if rx is not None else "None"
                log(f"{'OK' if ok_match else 'TIMEOUT'} {sender_id}->{receiver_id} | "
                    f"RSSI={rssi_txt} dBm | SNR={snr_txt} dB")

            try:
                store_measurement(cur, {
                    "pc_ts": now_iso(),
                    "round_id": round_id,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "ok_match": ok_match,
                    "node_timestamp": (rx["node_timestamp"] if rx else None),
                    "rssi": (rx["rssi"] if rx else None),
                    "snr": (rx["snr"] if rx else None),
                })
            except Exception as e:
                log(f"[DB] store_measurement failed: {e}")

        # Sender-LED wieder aus
        try:
            send_led_cmd(client, sender_id, False)
        except Exception as e:
            log(f"[Node {sender_id}] LED AUS failed: {e}")

    # Sicherheitshalber am Rundenende alle aus
    for sid in nodes:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    if aborted:
        conn.rollback()
        log(f"[LoRa round {round_id}] stopped, round discarded")
        return None

    duration_s = float(time.perf_counter() - t_round0)
    info = {
        "round_id": round_id,
        "pc_start": pc_start,
        "pc_end": now_iso(),
        "duration_s": duration_s,
        "nodes_csv": ",".join(str(n) for n in nodes),
        "ok_links": ok_links,
        "total_links": total_links,
        "timeouts": timeouts,
        "setting_id": setting_id,
    }

    try:
        store_round(cur, info)
        safe_commit(conn)
    except Exception as e:
        log(f"[DB] store_round/commit failed: {e}")

    log(f"[LoRa round {round_id}] ok={ok_links}/{total_links} timeouts={timeouts} dur={duration_s:.1f}s")
    return info


# =========================
# CW round (same flow as main_cw_data.py)
# =========================
def read_rssi_block(client, sid: int) -> Optional[Dict[str, Any]]:
    regs = read_regs(client, sid, REG_RSSI_BLOCK, RSSI_BLOCK_COUNT)
    if regs is None:
        return None
    return {"rssi": to_int16(regs[0]) / 10.0, "seq": regs[5]}


def measure_sender(client, sender_id: int, nodes: List[int], ctx) -> Dict[int, Optional[float]]:
    """One sender sends CW, all others measure. Returns {receiver_id: rssi or None}."""
    receivers = [sid for sid in nodes if sid != sender_id]
    result: Dict[int, Optional[float]] = {rid: None for rid in receivers}

    # Zählerstand vor der Messung – ein neuer Wert heißt: Ergebnis ist fertig
    seq0: Dict[int, int] = {}
    for rid in receivers:
        blk = read_rssi_block(client, rid)
        if blk is None:
            log(f"[RX Node {rid}] RSSI register not readable")
        else:
            seq0[rid] = blk["seq"]

    if not write_reg(client, sender_id, REG_CMD, CMD_CW_START):
        log(f"[TX Node {sender_id}] CW start failed")
        return result

    started = []
    for rid in seq0:
        if write_reg(client, rid, REG_CMD, CMD_RSSI_MEASURE):
            started.append(rid)
        else:
            log(f"[RX Node {rid}] measure start failed")

    deadline = time.time() + CW_MEASURE_MS / 1000.0 + CW_RESULT_TIMEOUT_S
    ctx.wait(CW_MEASURE_MS / 1000.0)

    pending = set(started)
    while pending and time.time() < deadline and not ctx.stopping():
        for rid in list(pending):
            blk = read_rssi_block(client, rid)
            if blk is not None and blk["seq"] != seq0[rid]:
                result[rid] = blk["rssi"]
                pending.discard(rid)
        if pending:
            time.sleep(CW_POLL_S)

    # Träger sofort stoppen, damit er die Messung des nächsten Senders nicht stört
    if not write_reg(client, sender_id, REG_CMD, CMD_CW_STOP):
        log(f"[TX Node {sender_id}] CW stop failed (stops by itself after {cw_tx_ms(len(nodes))} ms)")
        time.sleep(cw_tx_ms(len(nodes)) / 1000.0)

    return result


def cw_round(client, conn: sqlite3.Connection, round_id: int, radio_cfg: Dict[str, Any],
             nodes: List[int], ctx) -> Optional[Dict[str, Any]]:
    """
    One CW round among `nodes`; returns the stored round data, or None if ctx stops
    in the middle (the round is then discarded).
    """
    cur = conn.cursor()

    pc_start = now_iso()
    t_round0 = time.perf_counter()

    total_links = 0
    ok_links = 0

    if VERBOSE:
        log("")
        log(f"========== CW ROUND {round_id} START ==========")

    # CW- und Messdauer auf allen Nodes setzen (360, 361)
    for sid in nodes:
        if not write_regs(client, sid, REG_CW_TX_MS, [cw_tx_ms(len(nodes)), CW_MEASURE_MS]):
            log(f"[Node {sid}] CW duration write failed")

    for sender_id in nodes:
        if ctx.stopping():
            break

        result = measure_sender(client, sender_id, nodes, ctx)

        for receiver_id, rssi in result.items():
            total_links += 1
            if rssi is not None:
                ok_links += 1

            if VERBOSE and PRINT_LINK_LINE:
                rssi_txt = f"{rssi:.1f} dBm" if rssi is not None else "TIMEOUT"
                log(f"{sender_id}->{receiver_id} | RSSI={rssi_txt}")

            try:
                store_cw_measurement(cur, {
                    "pc_ts": now_iso(),
                    "round_id": round_id,
                    "sender_id": sender_id,
                    "receiver_id": receiver_id,
                    "rssi": rssi,
                })
            except Exception as e:
                log(f"[DB] store_cw_measurement failed: {e}")

    if ctx.stopping():
        # Sicherheitshalber alle Träger aus
        for sid in nodes:
            write_reg(client, sid, REG_CMD, CMD_CW_STOP)
        conn.rollback()
        log(f"[CW round {round_id}] stopped, round discarded")
        return None

    duration_s = float(time.perf_counter() - t_round0)
    info = {
        "round_id": round_id,
        "pc_start": pc_start,
        "pc_end": now_iso(),
        "duration_s": duration_s,
        "nodes_csv": ",".join(str(n) for n in nodes),
        "ok_links": ok_links,
        "total_links": total_links,
        "freq_mhz": radio_cfg.get("freq_mhz"),
        "tx_power": radio_cfg.get("tx_power"),
    }

    try:
        store_cw_round(cur, info)
        safe_commit(conn)
    except Exception as e:
        log(f"[DB] store_cw_round/commit failed: {e}")

    log(f"[CW round {round_id}] ok={ok_links}/{total_links} dur={duration_s:.1f}s")
    return info


# =========================
# Duty cycle
# =========================
def warn_if_duty_cycle_exceeded() -> None:
    """Worst case: every node transmits for the full CW duration in every CW round."""
    if CW_ROUNDS == 0:
        return
    tx_s = cw_tx_ms(len(NODES)) / 1000.0 * CW_ROUNDS
    period_s = MEASURE_INTERVAL_S * (LORA_ROUNDS + CW_ROUNDS)
    duty = tx_s / period_s
    if duty > DUTY_CYCLE_LIMIT:
        print(f"⚠️  Duty cycle: up to {tx_s:.1f} s of carrier per node every {period_s:.0f} s "
              f"= {duty * 100:.0f} % (limit {DUTY_CYCLE_LIMIT * 100:.0f} %). "
              f"Raise MEASURE_INTERVAL_S or lower CW_MEASURE_MS.")


# =========================
# Service
# =========================
def run(ctx, lora: str = "read") -> None:
    """
    Alternates LoRa and CW rounds until ctx stops.

    lora="read": no questions, radio settings are read from the first node
    lora="ask":  radio settings are asked on the terminal (default when started as a script)
    """
    if LORA_ROUNDS < 0 or CW_ROUNDS < 0 or LORA_ROUNDS + CW_ROUNDS == 0:
        raise RuntimeError("LORA_ROUNDS and CW_ROUNDS must not be negative and not both 0")

    print("=== LoRa / CW rotation logger ===")
    print(f"DB: {DB_PATH}")
    print(f"PORT: {PORT} @ {BAUDRATE}")
    print(f"NODES: {NODES}")
    print(f"Rotation: {LORA_ROUNDS} LoRa round(s), then {CW_ROUNDS} CW round(s)")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)

    client = ModbusSerialClient(
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity="N",
        timeout=1.0,
    )
    if not client.connect():
        raise RuntimeError("Modbus connect failed")

    try:
        conn = connect_db(DB_PATH, timeout_s=SQLITE_CONNECT_TIMEOUT_S)
    except Exception:
        client.close()
        raise
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS};")

    try:
        cur = conn.cursor()
        next_id = {}
        for kind, table in (("lora", ROUNDS_TABLE), ("cw", CW_ROUNDS_TABLE)):
            row = cur.execute(f"SELECT MAX(round_id) FROM {quote_ident(table)};").fetchone()
            next_id[kind] = int(row[0] or 0) + 1

        radio_cfg = configure_radio_interactive(client) if lora == "ask" else read_radio_config_or_defaults(client)
        ctx.report(radio=radio_summary(radio_cfg))

        setting_id = get_or_create_setting(cur, {
            "freq_mhz": radio_cfg.get("freq_mhz"),
            "tx_power": radio_cfg.get("tx_power"),
            "sf": radio_cfg.get("sf"),
            "bw_khz": radio_cfg.get("bw_khz"),
            "cr_denom": radio_cfg.get("cr_denom"),
            "lora_applied": radio_cfg.get("lora_applied", 0),
            "port": PORT,
            "baudrate": BAUDRATE,
            "measure_interval_s": MEASURE_INTERVAL_S,
        })
        safe_commit(conn)

        warn_if_duty_cycle_exceeded()

        pattern = ["lora"] * LORA_ROUNDS + ["cw"] * CW_ROUNDS
        step = 0

        print("\n=== Start rotation loop ===")
        print("Stop with Ctrl+C\n")

        while not ctx.stopping():
            t0 = time.perf_counter()
            started = datetime.now()
            kind = pattern[step % len(pattern)]
            step += 1
            mode = MODE_LORA if kind == "lora" else MODE_CW
            label = "LoRa" if kind == "lora" else "CW"

            ctx.report(activity=f"switching to {label} mode")
            nodes = sync_nodes(client, mode, ctx)
            if ctx.stopping():
                break

            if len(nodes) < 2:
                log(f"[{label}] only {len(nodes)} node(s) in {label} mode, round skipped")
                ctx.report(last_run=started, last_result=f"{label} round skipped: {len(nodes)} node(s) in mode")
            else:
                round_id = next_id[kind]
                ctx.report(activity=f"{label} round {round_id} running")
                if kind == "lora":
                    info = lora_round(client, conn, round_id, setting_id, nodes, ctx)
                else:
                    info = cw_round(client, conn, round_id, radio_cfg, nodes, ctx)
                if info is None:
                    break
                next_id[kind] += 1
                ctx.report(
                    last_run=started,
                    last_result=(
                        f"{label} round {round_id}: {info['ok_links']}/{info['total_links']} links, "
                        f"{info['duration_s']:.0f}s"
                    ),
                )

            dt = time.perf_counter() - t0
            sleep_s = max(0.0, float(MEASURE_INTERVAL_S) - dt)
            next_label = "LoRa" if pattern[step % len(pattern)] == "lora" else "CW"
            ctx.report(activity=f"waiting for {next_label} round",
                       next_run=datetime.now() + timedelta(seconds=sleep_s))
            ctx.wait(sleep_s)

    finally:
        try:
            stop_all(client)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def main():
    try:
        run(StandaloneContext(), lora="ask")
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
