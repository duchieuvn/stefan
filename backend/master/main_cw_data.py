"""CW-Logger für die slave_cw-Firmware: misst die RSSI (Signalstärke) zwischen allen Nodes.

Pro Runde sendet jeder Node einmal einen CW-Träger (CMD 20), alle anderen Nodes messen
gleichzeitig die RSSI (CMD 22). Ergebnis pro Link: sender_id, receiver_id, rssi (dBm).

Nur für Nodes mit firmware/slave_cw – die Paket-Firmware (slave, slave_fsk) misst
weiter mit main_senor_data.py.
"""
from __future__ import annotations

from pymodbus.client import ModbusSerialClient
import sqlite3
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/, for service_runtime
from service_runtime import ServiceContext

from config import *
from main_senor_data import (
    connect_db, quote_ident, safe_commit, log, to_int16, now_iso, _call,
    ask_int, ask_float, ask_yes_no,
)

# =========================
# slave_cw Register / Befehle (siehe firmware/slave_cw/modbus_handler.h)
# =========================
CMD_CW_START = 20
CMD_CW_STOP = 21
CMD_RSSI_MEASURE = 22

REG_CW_TX_MS = 330          # 330 = CW-Dauer ms, 331 = Messdauer ms
REG_RSSI_BLOCK = 114        # 114 = RSSI avg ×10 ... 119 = Mess-Zähler
RSSI_BLOCK_COUNT = 6
REG_FREQ = LORA_BASE        # 400 = Frequenz ×10, 401 = Leistung dBm
REG_APPLY = LORA_APPLY      # 410

# =========================
# CW-Einstellungen
# =========================
CW_MEASURE_MS = 1000        # so lange misst jeder Empfänger
CW_TX_MAX_MS = 10000        # Firmware-Limit
CW_RESULT_TIMEOUT_S = 3.0   # zusätzliche Wartezeit auf das Ergebnis nach Messende
CW_POLL_S = 0.1

# Träger muss laufen, bis der letzte Empfänger fertig gemessen hat:
# Messdauer + Zeit für die CMD-22-Writes an alle Empfänger + Reserve.
# Wird nach den Messungen mit CMD 21 ohnehin sofort gestoppt.
CW_TX_MS = min(CW_TX_MAX_MS, CW_MEASURE_MS + 150 * len(NODES) + 500)

# =========================
# SQLite
# =========================
CW_MEASUREMENT_TABLE = "cw_measurement"
CW_ROUNDS_TABLE = "cw_rounds"


def init_cw_db(db_path: str) -> None:
    con = connect_db(db_path, timeout_s=30.0)
    cur = con.cursor()

    cur.execute("PRAGMA busy_timeout = 15000;")
    try:
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA synchronous = NORMAL;")
    except Exception:
        pass

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


# =========================
# Modbus helpers
# =========================
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


def read_rssi_block(client, sid: int) -> Optional[Dict[str, Any]]:
    regs = read_regs(client, sid, REG_RSSI_BLOCK, RSSI_BLOCK_COUNT)
    if regs is None:
        return None
    return {"rssi": to_int16(regs[0]) / 10.0, "seq": regs[5]}


# =========================
# Frequenz / Leistung
# =========================
def radio_summary(cfg: Dict[str, Any]) -> str:
    return f"F={cfg['freq_mhz']:.1f} MHz, P={cfg['tx_power']} dBm"


def read_radio_config(client, sid: int) -> Optional[Dict[str, Any]]:
    regs = read_regs(client, sid, REG_FREQ, 2)
    if regs is None:
        return None
    return {"freq_mhz": regs[0] / 10.0, "tx_power": to_int16(regs[1])}


def apply_radio_config_to_all_nodes(client, cfg: Dict[str, Any]) -> bool:
    regs = [int(round(cfg["freq_mhz"] * 10.0)), int(cfg["tx_power"]) & 0xFFFF]
    ok_any = False
    for sid in NODES:
        if not write_regs(client, sid, REG_FREQ, regs):
            print(f"[Node {sid}] ❌ write {REG_FREQ}..{REG_FREQ + 1} fehlgeschlagen")
            continue
        if not write_reg(client, sid, REG_APPLY, 1):
            print(f"[Node {sid}] ❌ apply {REG_APPLY} fehlgeschlagen")
            continue
        ok_any = True
        print(f"[Node {sid}] ✅ Frequenz/Leistung gesetzt")
        time.sleep(0.1)
    time.sleep(0.5)
    return ok_any


def read_radio_config_or_defaults(client) -> Dict[str, Any]:
    cfg = read_radio_config(client, NODES[0])
    if cfg is not None:
        print(f"-> Gelesen von Node {NODES[0]}: {radio_summary(cfg)}")
        return cfg
    cfg = {"freq_mhz": DEFAULT_FREQ_MHZ, "tx_power": DEFAULT_POWER}
    print(f"-> Lesen fehlgeschlagen. Verwende Standardwerte: {radio_summary(cfg)}")
    return cfg


def configure_radio_interactive(client) -> Dict[str, Any]:
    print("\n=== CW Konfiguration ===")
    current = read_radio_config_or_defaults(client)

    if not ask_yes_no("Frequenz/Leistung jetzt auf alle Nodes schreiben?", default_yes=False):
        return current

    cfg = {
        "freq_mhz": ask_float("Frequenz MHz", current["freq_mhz"], 150.0, 960.0),
        "tx_power": ask_int("Power dBm", current["tx_power"], -9, 22),
    }
    print(f"\n-> Schreibe auf alle Nodes: {radio_summary(cfg)}")
    apply_radio_config_to_all_nodes(client, cfg)
    return cfg


# =========================
# Measurement loop
# =========================
def measure_sender(client, sender_id: int, ctx: ServiceContext) -> Dict[int, Optional[float]]:
    """Ein Sender schickt CW, alle anderen messen. Gibt {receiver_id: rssi oder None} zurück."""
    receivers = [sid for sid in NODES if sid != sender_id]
    result: Dict[int, Optional[float]] = {rid: None for rid in receivers}

    # Zählerstand vor der Messung – ein neuer Wert heißt: Ergebnis ist fertig
    seq0: Dict[int, int] = {}
    for rid in receivers:
        blk = read_rssi_block(client, rid)
        if blk is None:
            log(f"[RX Node {rid}] RSSI-Register nicht lesbar (slave_cw-Firmware?)")
        else:
            seq0[rid] = blk["seq"]

    if not write_reg(client, sender_id, CMD_REGISTER, CMD_CW_START):
        log(f"[TX Node {sender_id}] CW-Start fehlgeschlagen")
        return result

    started = []
    for rid in seq0:
        if write_reg(client, rid, CMD_REGISTER, CMD_RSSI_MEASURE):
            started.append(rid)
        else:
            log(f"[RX Node {rid}] Mess-Start fehlgeschlagen")

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
    if not write_reg(client, sender_id, CMD_REGISTER, CMD_CW_STOP):
        log(f"[TX Node {sender_id}] CW-Stop fehlgeschlagen (stoppt selbst nach {CW_TX_MS} ms)")
        time.sleep(CW_TX_MS / 1000.0)

    return result


def one_round(client, conn: sqlite3.Connection, round_id: int, radio_cfg: Dict[str, Any],
              ctx: ServiceContext) -> Optional[Dict[str, Any]]:
    """
    Eine Messrunde; gibt die gespeicherten Rundendaten zurück, oder None, wenn ctx
    mitten in der Runde stoppt (die Runde wird dann verworfen).
    """
    cur = conn.cursor()

    pc_start = now_iso()
    t_round0 = time.perf_counter()

    total_links = 0
    ok_links = 0

    if VERBOSE:
        log("")
        log(f"========== CW ROUND {round_id} START ==========")

    # CW- und Messdauer auf allen Nodes setzen (330, 331)
    for sid in NODES:
        if not write_regs(client, sid, REG_CW_TX_MS, [CW_TX_MS, CW_MEASURE_MS]):
            log(f"[Node {sid}] CW-Dauer write failed")

    for sender_id in NODES:
        if ctx.stopping():
            break

        result = measure_sender(client, sender_id, ctx)

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
        for sid in NODES:
            write_reg(client, sid, CMD_REGISTER, CMD_CW_STOP)
        conn.rollback()
        log(f"[CW Round {round_id}] gestoppt, Runde verworfen")
        return None

    duration_s = float(time.perf_counter() - t_round0)
    round_info = {
        "round_id": round_id,
        "pc_start": pc_start,
        "pc_end": now_iso(),
        "duration_s": duration_s,
        "nodes_csv": ",".join(str(n) for n in NODES),
        "ok_links": ok_links,
        "total_links": total_links,
        "freq_mhz": radio_cfg.get("freq_mhz"),
        "tx_power": radio_cfg.get("tx_power"),
    }

    try:
        store_cw_round(cur, round_info)
        safe_commit(conn)
    except Exception as e:
        log(f"[DB] store_cw_round/commit failed: {e}")

    log(f"[CW Round {round_id}] ok={ok_links}/{total_links} dur={duration_s:.1f}s")
    return round_info


def run(ctx: ServiceContext, radio: str = "ask") -> None:
    """
    Misst alle MEASURE_INTERVAL_S eine CW-Runde, bis ctx stoppt.

    radio="ask":  Frequenz/Leistung im Terminal abfragen (Standard beim direkten Start)
    radio="read": ohne Rückfragen, Werte aus dem ersten Node lesen
    """
    print("=== CW RSSI logger ===")
    print(f"DB: {DB_PATH}")
    print(f"PORT: {PORT} @ {BAUDRATE}")
    print(f"NODES: {NODES}")
    print(f"CW: TX {CW_TX_MS} ms, Messung {CW_MEASURE_MS} ms")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    init_cw_db(DB_PATH)

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
        row = cur.execute(f"SELECT MAX(round_id) FROM {quote_ident(CW_ROUNDS_TABLE)};").fetchone()
        next_round = int(row[0] or 0) + 1

        if radio == "ask":
            radio_cfg = configure_radio_interactive(client)
        else:
            radio_cfg = read_radio_config_or_defaults(client)
        ctx.report(radio=radio_summary(radio_cfg))

        print("\n=== Start CW measurement loop ===")
        print("Stop with Ctrl+C\n")

        while not ctx.stopping():
            t0 = time.perf_counter()
            started = datetime.now()
            ctx.report(activity=f"CW round {next_round} running")
            info = one_round(client, conn, next_round, radio_cfg, ctx)
            if info is None:
                break
            ctx.report(
                last_run=started,
                last_result=(
                    f"CW round {next_round}: {info['ok_links']}/{info['total_links']} links, "
                    f"{info['duration_s']:.0f}s"
                ),
            )
            next_round += 1

            dt = time.perf_counter() - t0
            sleep_s = max(0.0, float(MEASURE_INTERVAL_S) - dt)
            ctx.report(activity=f"waiting for CW round {next_round}",
                       next_run=datetime.now() + timedelta(seconds=sleep_s))
            ctx.wait(sleep_s)

    finally:
        # Kein Träger darf nach Ctrl+C weiterlaufen
        for sid in NODES:
            write_reg(client, sid, CMD_REGISTER, CMD_CW_STOP)
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
