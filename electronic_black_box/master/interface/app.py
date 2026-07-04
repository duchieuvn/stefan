from __future__ import annotations
from flask import Flask, request, render_template, Response
import sqlite3, time, logging, os
from pathlib import Path
from typing import Dict, Any, List

from interface import ModbusInterface  # dein Backend

logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL","INFO")))
log = logging.getLogger("modbus-web")

DB_PATH = Path(os.getenv("MB_DB", "modbus_data.db"))

# ---------- DB ----------
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS full_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slave_id INTEGER NOT NULL,
        own_id INTEGER,
        mac_ascii TEXT,
        rx_id INTEGER,
        recv_ts INTEGER,
        rssi REAL,
        snr REAL,
        tx_ts INTEGER,
        freq REAL,
        power INTEGER,
        sf INTEGER,
        bw INTEGER,
        cr INTEGER,
        read_at TEXT DEFAULT (datetime('now'))
    )""")
    con.commit()
    con.close()

def save_full_state(row: Dict[str,Any]):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("""
    INSERT INTO full_state (slave_id,own_id,mac_ascii,rx_id,recv_ts,rssi,snr,tx_ts,freq,power,sf,bw,cr)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        row["slave_id"], row["own_id"], row["mac_ascii"], row["rx_id"], row["recv_ts"],
        row["rssi"], row["snr"], row["tx_ts"], row["freq"], row["power"], row["sf"], row["bw"], row["cr"]
    ))
    con.commit(); con.close()

def latest_fullstates(n: int = 21):
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.execute("""SELECT id,slave_id,own_id,mac_ascii,rx_id,recv_ts,rssi,snr,tx_ts,freq,power,sf,bw,cr,read_at
                   FROM full_state ORDER BY id DESC LIMIT ?""", (n,))
    rows = cur.fetchall(); con.close()
    return rows

# ---------- UI-Helper ----------
def no_cache_html(html: str, status: int = 200) -> Response:
    resp = Response(html, status=status, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

def render_live_table(bridge: "ModbusInterface", ids: List[int]) -> str:
    if not bridge.latest:
        return '<div class="muted">Keine Daten…</div>'
    html = ['<table><thead><tr><th>Slave</th><th>MAC</th><th>rx_id</th><th>RSSI</th><th>SNR</th><th>recv_ts</th><th>Aktion</th></tr></thead><tbody>']
    for sid in ids:
        r = bridge.latest.get(sid, {"mac":"","rx_id":"","recv_ts":0,"rssi":0.0,"snr":0.0})
        html.append(
            f"<tr>"
            f"<td>{sid}</td>"
            f"<td><code>{r.get('mac','')}</code></td>"
            f"<td>{r.get('rx_id','')}</td>"
            f"<td>{r.get('rssi',0.0):.1f} dBm</td>"
            f"<td>{r.get('snr',0.0):.1f} dB</td>"
            f"<td>{r.get('recv_ts', 0)}</td>"
            f"<td>"
            f"<button hx-post='/api/measure' hx-vals='{{\"slave_id\":{sid}}}' hx-target='#toast'>Senden</button> "
            f"<button hx-post='/api/command' hx-vals='{{\"slave_id\":{sid},\"action\":\"led_on\"}}'  hx-target='#toast'>LED an</button> "
            f"<button hx-post='/api/command' hx-vals='{{\"slave_id\":{sid},\"action\":\"led_off\"}}' hx-target='#toast'>LED aus</button> "
            f"<button hx-post='/api/command' hx-vals='{{\"slave_id\":{sid},\"action\":\"reset\"}}'    hx-target='#toast'>Reset</button> "
            f"<button hx-get='/api/full/{sid}' hx-target='#toast'>Diagnose</button>"
            f"</td>"
            f"</tr>"
        )
    html.append("</tbody></table>")
    return "".join(html)

# ---------- Flask ----------
app = Flask(__name__)  # nutzt default ./templates und ./static
bridge = ModbusInterface()
bridge.start_poller()

# ---------- Pages ----------
@app.route("/")
def index():
    return render_template(
        "index.html",
        port=bridge.port,
        baud=bridge.baudrate,
        slaves=",".join(map(str, bridge.slave_ids))
    )

# ---------- API ----------
@app.get("/api/ping")
def api_ping():
    ok = bridge.ensure_connected()
    return f'<div id="ping">Status: <b>{"verbunden" if ok else "getrennt"}</b></div>'

@app.get("/api/read/compact")
def api_read_compact():
    html = render_live_table(bridge, bridge.slave_ids)
    return no_cache_html(html)

@app.get("/api/full/<int:slave_id>")
def api_full(slave_id: int):
    row = bridge.read_full_state(slave_id)
    if not row:
        return f"❌ Keine Daten von Slave {slave_id}", 502
    return (
        "🛰️ Diagnose " + str(slave_id) + "<br>"
        f"own_id={row['own_id']}, mac={row['mac_ascii']}<br>"
        f"rx_id={row['rx_id']}, recv_ts={row['recv_ts']}, RSSI={row['rssi']:.1f}, SNR={row['snr']:.1f}<br>"
        f"tx_ts={row['tx_ts']}, LoRa: freq={row['freq']:.1f} MHz, pwr={row['power']}, sf={row['sf']}, bw={row['bw']}, cr={row['cr']}"
    )

@app.post("/api/measure")
def api_measure():
    data = request.get_json(silent=True) or request.form or {}
    try:
        slave_id = int(data.get("slave_id"))
    except Exception:
        return "Bad payload (slave_id fehlt)", 400
    ok = bridge.run_measure_cycle_for(slave_id)
    live_html = render_live_table(bridge, bridge.slave_ids)
    toast = f"✅ Gesendet von {slave_id} & Live aktualisiert" if ok else f"⚠️ Senden {slave_id} fehlgeschlagen"
    return no_cache_html(
        f'<div id="toast">{toast}</div>'
        f'<div id="live" hx-swap-oob="true">{live_html}</div>'
    )

@app.post("/api/command")
def api_command():
    data = request.get_json(silent=True) or request.form or {}
    try:
        slave_id = int(data.get("slave_id"))
        action   = str(data.get("action"))
    except Exception:
        return "Bad payload", 400
    ok = bridge.send_command(slave_id, action)
    return ("✅ OK" if ok else "❌ Modbus write failed"), (200 if ok else 502)

@app.post("/api/broadcast")
def api_broadcast():
    data = request.get_json(silent=True) or request.form or {}
    action = (data.get("action") or "").strip()
    ids = data.get("ids") or bridge.slave_ids
    if isinstance(ids, str):
        ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    ok = bridge.broadcast(action, ids)
    return f"Broadcast {action}: {ok}/{len(ids)} ok"

@app.post("/api/live/reload")
def api_live_reload():
    live_html = render_live_table(bridge, bridge.slave_ids)
    return no_cache_html(
        f'<div id="toast">🔄 Live aktualisiert</div>'
        f'<div id="live" hx-swap-oob="true">{live_html}</div>'
    )

@app.post("/api/lora/apply")
def api_lora_apply():
    data = request.get_json(silent=True) or request.form or {}
    try:
        freq_mhz  = float(data.get("freq_mhz"))
        power_dbm = int(data.get("power_dbm"))
        sf        = int(data.get("sf"))
        bw_khz    = int(data.get("bw_khz"))
        cr_denom  = int(data.get("cr_denom"))
    except Exception:
        return "Bad payload (Parameter fehlen/ungültig)", 400

    ids_raw = data.get("ids")
    if ids_raw:
        try:
            target_ids = [int(x) for x in str(ids_raw).split(",") if x.strip().isdigit()]
        except Exception:
            return "Bad ids", 400
        if not target_ids:
            target_ids = bridge.slave_ids
    else:
        target_ids = bridge.slave_ids

    ok = 0
    for sid in target_ids:
        if bridge.write_lora_config_for(sid, freq_mhz=freq_mhz, power_dbm=power_dbm, sf=sf, bw_khz=bw_khz, cr_denom=cr_denom):
            ok += 1
        time.sleep(0.08)

    return f"LoRa gesetzt ({ok}/{len(target_ids)} ok) — Freq={freq_mhz:.1f} MHz, Pwr={power_dbm} dBm, SF={sf}, BW={bw_khz} kHz, CR=4/{cr_denom}"

@app.post("/api/collect")
def api_collect():
    saved = 0
    for sid in bridge.slave_ids:
        row = bridge.read_full_state(sid)
        if row:
            save_full_state(row); saved += 1
            time.sleep(0.05)
    return f"💾 Saved {saved}/{len(bridge.slave_ids)} Datensätze."

@app.get("/api/db/latest")
def api_db_latest():
    try:
        n = int(request.args.get("n", "21"))
    except Exception:
        n = 21
    rows = latest_fullstates(n)
    if not rows:
        return no_cache_html('<div class="muted">DB leer.</div>')
    header = ["id","slave","own","mac","rx","recv_ts","rssi","snr","tx_ts","freq","pwr","sf","bw","cr","read_at"]
    html = ['<table><thead><tr>'] + [f"<th>{h}</th>" for h in header] + ['</tr></thead><tbody>']
    for r in rows:
        html.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    html.append("</tbody></table>")
    return no_cache_html("".join(html))

@app.get("/api/diag")
def api_diag():
    info = {
        "connected": bridge.ensure_connected(),
        "poller_running": bridge.is_poller_running(),
        "port": bridge.port,
        "baud": bridge.baudrate,
        "slaves": bridge.slave_ids,
        "cache_keys": sorted(list(bridge.latest.keys())),
    }
    return info, 200

@app.post("/api/diag/probe")
def api_diag_probe():
    res = bridge.diag_probe()
    items = "".join(f"<li>Slave {sid}: {st}</li>" for sid, st in res["details"])
    return no_cache_html(f"""
    <div id="toast">Probe: ok={res['ok']} fail={res['fail']} (connected={res['connected']})</div>
    <ul>{items}</ul>
    """)

@app.post("/api/diag/tick")
def api_diag_tick():
    n = bridge.tick_once()
    live_html = render_live_table(bridge, bridge.slave_ids)
    return no_cache_html(
        f'<div id="toast">Tick: {n} Slaves gelesen</div>'
        f'<div id="live" hx-swap-oob="true">{live_html}</div>'
    )

# ---------- Main ----------
if __name__ == "__main__":
    init_db()
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("WEB_PORT","8080")), debug=False, use_reloader=False, threaded=True)
    finally:
        bridge.stop()
