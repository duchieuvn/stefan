from __future__ import annotations

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
import sqlite3
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager"))  # for service_runtime
from service_runtime import ServiceContext

from config import *

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

            ok_match INTEGER,

            rx_id INTEGER,
            node_timestamp INTEGER,

            rssi REAL,
            snr REAL
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
            marker,
            ok_match, rx_id, node_timestamp, rssi, snr
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        row.get("pc_ts"),
        row.get("round_id"), row.get("sender_id"), row.get("receiver_id"),
        row.get("marker"),
        row.get("ok_match"), row.get("rx_id"), row.get("node_timestamp"),
        row.get("rssi"), row.get("snr"),
    ))


def store_round(cur: sqlite3.Cursor, info: Dict[str, Any]) -> None:
    cur.execute(f"""
        INSERT OR REPLACE INTO {quote_ident(ROUNDS_TABLE)} (
            round_id,
            pc_start, pc_end, duration_s,

            nodes_csv,

            ok_links, total_links, timeouts,

            freq_mhz, tx_power, sf, bw_khz, cr_denom, lora_applied,

            port, baudrate, measure_interval_s, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        info.get("round_id"),
        info.get("pc_start"), info.get("pc_end"), info.get("duration_s"),

        info.get("nodes_csv"),

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


def ask_int(prompt: str, default: int, min_v: int, max_v: int) -> int:
    while True:
        s = input(f"{prompt} [{default}]: ").strip()
        if s == "":
            return default
        try:
            v = int(s)
            if not (min_v <= v <= max_v):
                print(f"  -> bitte zwischen {min_v} und {max_v}")
                continue
            return v
        except ValueError:
            print("  -> bitte eine ganze Zahl eingeben")


def ask_float(prompt: str, default: float, min_v: float, max_v: float) -> float:
    while True:
        s = input(f"{prompt} [{default}]: ").strip()
        if s == "":
            return float(default)
        try:
            v = float(s.replace(",", "."))
            if not (min_v <= v <= max_v):
                print(f"  -> bitte zwischen {min_v} und {max_v}")
                continue
            return v
        except ValueError:
            print("  -> bitte eine Zahl eingeben")


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    while True:
        s = input(f"{prompt} ({d}): ").strip().lower()
        if s == "":
            return default_yes
        if s in ("y", "yes", "j", "ja"):
            return True
        if s in ("n", "no", "nein"):
            return False
        print("  -> bitte y/n")


# =========================
# LoRa config read/write
# =========================
def read_lora_config(client, sid: int) -> Optional[Dict[str, Any]]:
    """
    Reads 400..404 from one node:
      400=freq_x10, 401=power, 402=sf, 403=bw_khz, 404=cr_denom
    """
    try:
        res = _call(client.read_holding_registers, address=LORA_BASE, count=5, device_id=sid)
        if res is None or res.isError():
            return None

        regs = res.registers
        if len(regs) < 5:
            return None

        freq_x10 = int(regs[0])
        power = int(regs[1])
        sf = int(regs[2])
        bw = int(regs[3])
        cr = int(regs[4])

        return {
            "freq_mhz": float(freq_x10) / 10.0,
            "tx_power": power,
            "sf": sf,
            "bw_khz": bw,
            "cr_denom": cr,
            "lora_applied": 1,
        }
    except Exception:
        return None


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
                print(f"[Node {sid}] ❌ write 400..404 fehlgeschlagen")
                continue

            a = _call(client.write_register, LORA_APPLY, 1, device_id=sid)
            if a is None or a.isError():
                print(f"[Node {sid}] ❌ apply 410 fehlgeschlagen")
                continue

            ok_any = True
            print(f"[Node {sid}] ✅ LoRa gesetzt")
            time.sleep(0.1)
        except Exception as e:
            print(f"[Node {sid}] ❌ exception: {e}")

    time.sleep(1.0)
    return ok_any


def ask_lora_config_manually(default_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    default_cfg = default_cfg or {}
    freq_mhz = ask_float("Frequenz MHz", float(default_cfg.get("freq_mhz", DEFAULT_FREQ_MHZ)), 100.0, 1000.0)
    power = ask_int("Power", int(default_cfg.get("tx_power", DEFAULT_POWER)), 0, 30)
    sf = ask_int("Spreading Factor (SF)", int(default_cfg.get("sf", DEFAULT_SF)), 6, 12)
    bw = ask_int("Bandwidth (kHz)", int(default_cfg.get("bw_khz", DEFAULT_BW)), 7, 500)
    cr = ask_int("Coding Rate Nenner (CR=4/x)", int(default_cfg.get("cr_denom", DEFAULT_CR)), 5, 8)

    return {
        "freq_mhz": float(freq_mhz),
        "tx_power": int(power),
        "sf": int(sf),
        "bw_khz": int(bw),
        "cr_denom": int(cr),
        "lora_applied": 0,
    }


def lora_summary(cfg: Dict[str, Any]) -> str:
    return (
        f"F={cfg['freq_mhz']:.1f} MHz, P={cfg['tx_power']}, "
        f"SF{cfg['sf']}, BW={cfg['bw_khz']}, CR=4/{cfg['cr_denom']}"
    )


def configure_lora_interactive(client) -> Dict[str, Any]:
    """
    1) Entweder LoRa jetzt auf alle Nodes schreiben
    2) oder aktuelle Werte aus Node 1 lesen
    3) oder manuell eintragen, wenn Lesen fehlschlägt
    """
    print("\n=== LoRa Konfiguration ===")

    if ask_yes_no("LoRa Parameter jetzt aktiv auf alle Nodes schreiben?", default_yes=True):
        cfg = ask_lora_config_manually()
        print(f"\n-> Schreibe auf alle Nodes: {lora_summary(cfg)}")
        ok = apply_lora_config_to_all_nodes(client, cfg)
        cfg["lora_applied"] = 1 if ok else 0
        return cfg

    print("-> LoRa wird nicht neu gesetzt.")
    print(f"-> Versuche aktuelle LoRa-Werte von Node {NODES[0]} zu lesen ...")
    cfg = read_lora_config(client, NODES[0])

    if cfg is not None:
        print(f"-> Gelesen: {lora_summary(cfg)}")
        return cfg

    print("-> Lesen fehlgeschlagen. Bitte aktuelle Werte manuell eingeben.")
    cfg = ask_lora_config_manually()
    cfg["lora_applied"] = 0
    return cfg


def read_lora_config_or_defaults(client) -> Dict[str, Any]:
    """
    Ohne Rückfragen: LoRa nicht neu setzen, aktuelle Werte aus Node 1 lesen,
    bei Fehler die Standardwerte annehmen.
    """
    print("\n=== LoRa Konfiguration ===")
    print("-> LoRa wird nicht neu gesetzt.")
    print(f"-> Versuche aktuelle LoRa-Werte von Node {NODES[0]} zu lesen ...")
    cfg = read_lora_config(client, NODES[0])

    if cfg is not None:
        print(f"-> Gelesen: {lora_summary(cfg)}")
        return cfg

    cfg = {
        "freq_mhz": DEFAULT_FREQ_MHZ,
        "tx_power": DEFAULT_POWER,
        "sf": DEFAULT_SF,
        "bw_khz": DEFAULT_BW,
        "cr_denom": DEFAULT_CR,
        "lora_applied": 0,
    }
    print(f"-> Lesen fehlgeschlagen. Verwende Standardwerte: {lora_summary(cfg)}")
    return cfg


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

def send_led_cmd(client, sid: int, on: bool) -> None:
    cmd = CMD_LED_ON if on else CMD_LED_OFF
    w = _call(client.write_register, CMD_REGISTER, cmd, device_id=sid)
    if w is None or w.isError():
        raise RuntimeError(f"[Node {sid}] LED command failed")


# =========================
# Measurement loop
# =========================
def one_round(client, conn: sqlite3.Connection, round_id: int, lora_cfg: Dict[str, Any],
              ctx: ServiceContext) -> Optional[Dict[str, Any]]:
    """
    Eine Messrunde; gibt die gespeicherten Rundendaten zurück, oder None, wenn ctx
    mitten in der Runde stoppt (die Runde wird dann verworfen).
    """
    cur = conn.cursor()

    pc_start = now_iso()
    t_round0 = time.perf_counter()

    nodes_csv = ",".join(str(n) for n in NODES)

    total_links = 0
    ok_links = 0
    timeouts = 0

    if VERBOSE:
        log("")
        log(f"========== ROUND {round_id} START ==========")

    for sid in NODES:
        try:
            write_payload(client, sid, PAYLOAD_TEXT)
        except Exception as e:
            log(f"[Node {sid}] payload write failed: {e}")

    # Sicherheitshalber alle LEDs aus
    for sid in NODES:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    aborted = False
    for sender_id in NODES:
        if ctx.stopping():
            aborted = True
            break

        tx_timestamp = make_tx_timestamp_31()

        # Nur aktueller Sender leuchtet
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
            log(f"[Node {sender_id}] LED AN failed: {e}")

        try:
            write_tx_timestamp(client, sender_id, tx_timestamp)

            t_send0 = time.perf_counter()
            send_tx_cmd(client, sender_id)
            t_send_done = time.perf_counter()
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

            while time.time() < deadline and not ctx.stopping():
                rx = rx_block_stable(client, receiver_id)
                if rx is None:
                    time.sleep(RX_POLL_S)
                    continue

                id_match_now = int(rx.get("rx_id", -1)) == int(sender_id)
                ts_match_now = int(rx.get("node_timestamp", -1)) == int(tx_timestamp)

                if id_match_now and ts_match_now:
                    break

                time.sleep(RX_POLL_S)

            if ctx.stopping():
                aborted = True
                break

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

            

            if VERBOSE and PRINT_LINK_LINE:
                rssi_txt = f"{rx.get('rssi'):.1f}" if (rx is not None and rx.get("rssi") is not None) else "None"
                snr_txt = f"{rx.get('snr'):.1f}" if (rx is not None and rx.get("snr") is not None) else "None"

                status = "OK" if ok_match else "TIMEOUT"

                log(
                    f"{status} {sender_id}->{receiver_id} | "
                    f"RSSI={rssi_txt} dBm | "
                    f"SNR={snr_txt} dB | "
                    f"rx_wait={rx_wait_ms:.1f} ms | "
                    f"e2e={e2e_ms:.1f} ms | "
                )

            row = {
                "pc_ts": now_iso(),
                "round_id": round_id,
                "sender_id": sender_id,
                "receiver_id": receiver_id,

                # marker-Feld bleibt erhalten, enthält jetzt aber den gesendeten TX-Timestamp
                "marker": tx_timestamp,

                "ok_match": ok_match,

                "rx_id": (rx.get("rx_id") if rx else None),
                "node_timestamp": (rx.get("node_timestamp") if rx else None),

                "rssi": (rx.get("rssi") if rx else None),
                "snr": (rx.get("snr") if rx else None),
            }

            try:
                store_measurement(cur, row)
            except Exception as e:
                log(f"[DB] store_measurement failed: {e}")

        # Sender-LED wieder aus
        try:
            send_led_cmd(client, sender_id, False)
        except Exception as e:
            log(f"[Node {sender_id}] LED AUS failed: {e}")

    # Sicherheitshalber am Rundenende alle aus
    for sid in NODES:
        try:
            send_led_cmd(client, sid, False)
        except Exception:
            pass

    if aborted:
        conn.rollback()
        log(f"[Round {round_id}] gestoppt, Runde verworfen")
        return None

    pc_end = now_iso()
    duration_s = float(time.perf_counter() - t_round0)

    round_info = {
        "round_id": round_id,
        "pc_start": pc_start,
        "pc_end": pc_end,
        "duration_s": duration_s,

        "nodes_csv": nodes_csv,

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
        "notes": "",
    }

    try:
        store_round(cur, round_info)
        safe_commit(conn)
    except Exception as e:
        log(f"[DB] store_round/commit failed: {e}")

    log(f"[Round {round_id}] ok={ok_links}/{total_links} timeouts={timeouts} dur={duration_s:.1f}s")
    return round_info


def run(ctx: ServiceContext, lora: str = "ask") -> None:
    """
    Misst alle MEASURE_INTERVAL_S eine Runde, bis ctx stoppt.

    lora="ask":  LoRa-Fragen im Terminal stellen (Standard beim direkten Start)
    lora="read": ohne Rückfragen, LoRa-Werte aus Node 1 lesen (für manage_services.py)
    """
    print("=== monitor_offline logger ===")
    print(f"DB: {DB_PATH}")
    print(f"PORT: {PORT} @ {BAUDRATE}")
    print(f"NODES: {NODES}")

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
        try:
            row = cur.execute("SELECT MAX(round_id) FROM rounds;").fetchone()
            next_round = int(row[0] or 0) + 1
        except Exception:
            next_round = 1

        if lora == "ask":
            lora_cfg = configure_lora_interactive(client)
        else:
            lora_cfg = read_lora_config_or_defaults(client)
        ctx.report(lora=lora_summary(lora_cfg))

        print("\n=== Start measurement loop ===")
        print("Stop with Ctrl+C\n")

        while not ctx.stopping():
            t0 = time.perf_counter()
            started = datetime.now()
            ctx.report(activity=f"round {next_round} running")
            info = one_round(client, conn, next_round, lora_cfg, ctx)
            if info is None:
                break
            ctx.report(
                last_run=started,
                last_result=(
                    f"round {next_round}: ok {info['ok_links']}/{info['total_links']} links, "
                    f"{info['timeouts']} timeouts, {info['duration_s']:.0f}s"
                ),
            )
            next_round += 1

            dt = time.perf_counter() - t0
            sleep_s = max(0.0, float(MEASURE_INTERVAL_S) - dt)
            ctx.report(activity=f"waiting for round {next_round}",
                       next_run=datetime.now() + timedelta(seconds=sleep_s))
            ctx.wait(sleep_s)

    finally:
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
        run(ServiceContext.standalone())
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
