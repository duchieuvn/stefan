# modbus_web.py  —  stabile Broadcasts + gelockter Poller
from __future__ import annotations
from flask import Flask, request, render_template_string, Response
from pymodbus.client import ModbusSerialClient
import sqlite3, time, logging, threading, os, glob
from pathlib import Path
from typing import Optional, List, Dict, Any

# ---------- KONFIG ----------
def autodetect_port() -> str | None:
    for pat in ("/dev/cu.usbserial*", "/dev/tty.usbserial*", "/dev/ttyUSB*", "/dev/cu.usbmodem*"):
        m = sorted(glob.glob(pat))
        if m:
            return m[0]
    return None

PORT      = os.getenv("MB_PORT", autodetect_port() or "/dev/cu.usbserial-110")
BAUDRATE  = int(os.getenv("MB_BAUDRATE", "9600"))
CMD_REGISTER = 200
SLAVE_IDS: List[int] = [1,2,3,4,5,6,7]

# Poller/Write Tuning
CLIENT_TIMEOUT_S     = float(os.getenv("MB_TIMEOUT", "0.6"))
CLIENT_RETRIES       = int(os.getenv("MB_RETRIES", "0"))
READ_GAP_S           = float(os.getenv("MB_READ_GAP", "0.07"))
POLL_CYCLE_PAUSE_S   = float(os.getenv("MB_CYCLE_PAUSE", "0.2"))
DEFAULT_WAIT_MS      = int(os.getenv("MB_WAIT_MS", "1200"))

# Neue, wichtige Broadcast/Write-Parameter
WRITE_RETRIES        = int(os.getenv("MB_WRITE_RETRIES", "2"))     # zusätzliche Versuche pro Slave
WRITE_RETRY_DELAY_S  = float(os.getenv("MB_WRITE_RETRY_DELAY", "0.08"))
BROADCAST_GAP_S      = float(os.getenv("MB_BROADCAST_GAP", "0.08"))  # Pause zwischen zwei Slaves bei Broadcast
LORA_GAP_S           = float(os.getenv("MB_LORA_GAP", "0.08"))       # Pause zwischen zwei Slaves bei LoRa-Apply

DB_PATH   = Path(os.getenv("MB_DB", "modbus_data.db"))

logging.getLogger("pymodbus").setLevel(logging.ERROR)
log = logging.getLogger("modbus-web"); log.setLevel(logging.INFO)

# ---------- MODBUS ----------
client = ModbusSerialClient(
    port=PORT, baudrate=BAUDRATE, stopbits=1, bytesize=8, parity="N",
    timeout=CLIENT_TIMEOUT_S, retries=CLIENT_RETRIES
)
_client_lock = threading.RLock()
_last_try = {"t": 0.0}

def ensure_connected() -> bool:
    if client.connected:
        return True
    now = time.monotonic()
    if now - _last_try["t"] < 1.0:
        return client.connected
    _last_try["t"] = now
    try:
        ok = client.connect()
        if ok:
            log.info("✅ Modbus verbunden (%s @ %s baud)", PORT, BAUDRATE)
        return ok
    except Exception as e:
        log.warning("connect failed: %s", e)
        return False

# Low-level ohne Lock (Lock im Aufrufer halten!)
def _write_reg_nolock(addr: int, val: int, slave: int) -> bool:
    if not ensure_connected(): return False
    try:
        res = client.write_register(addr, val, slave=slave)
        return bool(res) and not res.isError()
    except Exception:
        try: client.close(); client.connect()
        except: pass
        try:
            res = client.write_register(addr, val, slave=slave)
            return bool(res) and not res.isError()
        except Exception:
            return False

def _write_regs_nolock(addr: int, vals: List[int], slave: int) -> bool:
    if not ensure_connected(): return False
    try:
        res = client.write_registers(addr, vals, slave=slave)
        return bool(res) and not res.isError()
    except Exception:
        try: client.close(); client.connect()
        except: pass
        try:
            res = client.write_registers(addr, vals, slave=slave)
            return bool(res) and not res.isError()
        except Exception:
            return False

def _read_regs_nolock(addr: int, count: int, slave: int) -> Optional[List[int]]:
    if not ensure_connected(): return None
    try:
        res = client.read_holding_registers(address=addr, count=count, slave=slave)
        if not res or res.isError(): return None
        return res.registers
    except Exception:
        try: client.close(); client.connect()
        except: pass
        try:
            res = client.read_holding_registers(address=addr, count=count, slave=slave)
            if not res or res.isError(): return None
            return res.registers
        except Exception:
            return None

# Hilfs-Wrapper mit Retry (Lock MUSS gehalten werden!)
def _write_reg_retry(addr: int, val: int, slave: int) -> bool:
    attempts = 1 + WRITE_RETRIES
    for i in range(attempts):
        ok = _write_reg_nolock(addr, val, slave)
        if ok:
            return True
        time.sleep(WRITE_RETRY_DELAY_S)
    return False

def _write_regs_retry(addr: int, vals: List[int], slave: int) -> bool:
    attempts = 1 + WRITE_RETRIES
    for i in range(attempts):
        ok = _write_regs_nolock(addr, vals, slave)
        if ok:
            return True
        time.sleep(WRITE_RETRY_DELAY_S)
    return False

# ---------- DB ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
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
    con.commit(); con.close()
    log.info("DB: %s", DB_PATH.resolve())

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

# ---------- Cache + Poller ----------
def signed16(v:int)->int: return v if v<32768 else v-65536

LATEST: Dict[int, Dict[str, Any]] = {}
MAC_CACHE: Dict[int, str] = {}
PRIORITY: Dict[str, Any] = {"slave": None, "until": 0.0}

def _read_compact_once_locked(slave_id:int) -> Optional[Dict[str,Any]]:
    """Lock MUSS gehalten werden."""
    r2 = _read_regs_nolock(100, 16, slave_id)
    if not r2:
        return None
    rx_id   = r2[0]
    recv_ts = (r2[12] << 16) | r2[13]
    rssi    = signed16(r2[14]) / 10.0
    snr     = signed16(r2[15]) / 10.0
    if slave_id not in MAC_CACHE:
        r1 = _read_regs_nolock(10, 22, slave_id)
        MAC_CACHE[slave_id] = "".join(chr(c) for c in r1[10:22]) if r1 else ""
    return {
        "slave_id": slave_id,
        "mac": MAC_CACHE.get(slave_id, ""),
        "rx_id": rx_id,
        "recv_ts": recv_ts,
        "rssi": rssi,
        "snr": snr,
        "t_read": time.time(),
    }

def poller_loop():
    log.info("🌀 Poller gestartet.")
    while True:
        try:
            ensure_connected()
            ids = SLAVE_IDS[:]
            now = time.monotonic()
            prio = PRIORITY.get("slave")
            prio_until = PRIORITY.get("until", 0.0)
            if prio and now < prio_until:
                ids = [prio] + [i for i in ids if i != prio]
            else:
                PRIORITY["slave"] = None
                PRIORITY["until"] = 0.0

            for sid in ids:
                with _client_lock:
                    data = _read_compact_once_locked(sid)
                    if data:
                        LATEST[sid] = data
                time.sleep(READ_GAP_S)
            time.sleep(POLL_CYCLE_PAUSE_S)
        except Exception as e:
            log.warning("Poller-Fehler: %s", e)
            time.sleep(0.5)

# ---------- High-level Ops ----------
def write_timestamp(slave_id:int, ts:int)->bool:
    hi, lo = (ts>>16)&0xFFFF, ts&0xFFFF
    with _client_lock:
        return _write_regs_retry(300, [hi,lo], slave_id)

def send_command(slave_id:int, action:str)->bool:
    mapping = {"led_on":1, "led_off":2, "reset":99}
    val = mapping.get(action)
    if val is None: return False
    with _client_lock:
        return _write_reg_retry(CMD_REGISTER, val, slave_id)

def read_full_state(slave_id:int)->Optional[Dict[str,Any]]:
    with _client_lock:
        r1=_read_regs_nolock(10,22,slave_id)
        r2=_read_regs_nolock(100,16,slave_id)
        r3=_read_regs_nolock(300,2,slave_id)
        r4=_read_regs_nolock(400,5,slave_id)
    if not (r1 and r2 and r3 and r4): return None
    return {
        "slave_id": slave_id,
        "own_id": r1[0],
        "mac_ascii": "".join(chr(c) for c in r1[10:22]),
        "rx_id": r2[0],
        "recv_ts": (r2[12]<<16)|r2[13],
        "rssi": signed16(r2[14])/10.0,
        "snr":  signed16(r2[15])/10.0,
        "tx_ts": (r3[0]<<16)|r3[1],
        "freq": r4[0]/10.0,
        "power": r4[1],
        "sf": r4[2],
        "bw": r4[3],
        "cr": r4[4],
    }

def collect_and_store_all()->Dict[str,Any]:
    items=[]; ok=0
    for sid in SLAVE_IDS:
        row = read_full_state(sid)
        if row:
            save_full_state(row); ok+=1; items.append(row)
        time.sleep(0.05)
    return {"saved":ok,"total":len(SLAVE_IDS),"items":items}

def write_lora_config_for(slave_id:int, *, freq_mhz:float, power_dbm:int, sf:int, bw_khz:int, cr_denom:int) -> bool:
    if not (860.0 <= freq_mhz <= 930.0): return False
    if not (0 <= power_dbm <= 22): return False
    if sf not in (7,8,9,10,11,12): return False
    if bw_khz not in (125, 250, 500): return False
    if cr_denom not in (5,6,7,8): return False
    regs = [int(round(freq_mhz * 10)), int(power_dbm), int(sf), int(bw_khz), int(cr_denom)]
    with _client_lock:
        return _write_regs_retry(400, regs, slave_id)

def run_measure_cycle_for(slave_id: int, wait_ms: int = DEFAULT_WAIT_MS) -> bool:
    ts = int(time.time())
    ts_ok = write_timestamp(slave_id, ts)
    with _client_lock:
        meas_ok = _write_reg_retry(CMD_REGISTER, 10, slave_id)
    PRIORITY["slave"] = slave_id
    PRIORITY["until"] = time.monotonic() + max(wait_ms/1000.0, 0.8)
    time.sleep(max(wait_ms, 0) / 1000.0)
    return bool(ts_ok and meas_ok)

# ---------- UI ----------
def render_live_table_from_cache():
    if not LATEST:
        return '<div class="muted">Keine Daten…</div>'
    ids = [sid for sid in SLAVE_IDS]
    html = ['<table><thead><tr><th>Slave</th><th>MAC</th><th>rx_id</th><th>RSSI</th><th>SNR</th><th>recv_ts</th><th>Aktion</th></tr></thead><tbody>']
    for sid in ids:
        r = LATEST.get(sid, {"mac":"","rx_id":"","recv_ts":0,"rssi":0.0,"snr":0.0})
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

def _get_payload() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if isinstance(data, dict): return data
    if request.form: return request.form.to_dict()
    return {}

def no_cache_html(html: str, status: int = 200) -> Response:
    resp = Response(html, status=status, mimetype="text/html; charset=utf-8")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

# ---------- Flask ----------
app = Flask(__name__)

INDEX_HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Modbus Web</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <script src="https://unpkg.com/hyperscript.org@0.9.12"></script>
  <script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/json-enc.js"></script>
  <style>
    body{font-family:ui-sans-serif,system-ui;max-width:1100px;margin:24px auto;padding:0 16px}
    .card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,0.04)}
    button{padding:8px 12px;border:1px solid #ccc;border-radius:10px;background:#fff;cursor:pointer}
    button:hover{background:#f6f6f6}
    input,select{padding:6px 8px;border:1px solid #ccc;border-radius:10px}
    label{font-size:12px;color:#444;margin-right:6px}
    table{width:100%;border-collapse:collapse}
    th,td{border-bottom:1px solid #eee;padding:8px;text-align:left;font-size:14px}
    .row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .pill{display:inline-block;border:1px solid #ddd;border-radius:999px;padding:6px 10px;margin-right:6px}
    .muted{color:#666}
    code{background:#f6f6f6;padding:2px 6px;border-radius:6px}
  </style>
</head>
<body>

<h1>Modbus Web</h1>

<div class="card">
  <div class="row">
    <div class="pill">Port: <code>{{port}}</code></div>
    <div class="pill">Baudrate: <code>{{baud}}</code></div>
    <div class="pill">Slaves: <code>{{slaves}}</code></div>
  </div>
  <div id="ping" hx-get="/api/ping" hx-trigger="every 3s" hx-swap="outerHTML">
    <div class="muted">Status wird geladen…</div>
  </div>
</div>

<div class="card" hx-ext="json-enc">
  <h3>Aktionen</h3>
  <div class="row" style="margin-bottom:8px">
    <button hx-post="/api/live/reload" hx-target="#toast">🔄 Live neu laden</button>
    <button hx-post="/api/broadcast" hx-vals='{"action":"led_on"}'  hx-target="#toast">LED an (alle)</button>
    <button hx-post="/api/broadcast" hx-vals='{"action":"led_off"}' hx-target="#toast">LED aus (alle)</button>
    <button hx-post="/api/broadcast" hx-vals='{"action":"reset"}'   hx-target="#toast">Reset (alle)</button>
    <button hx-post="/api/collect"  hx-target="#toast">💾 Full-State speichern</button>
    <button hx-get="/api/db/latest?n=21" hx-target="#db">DB: letzte 21</button>
  </div>

  <h4>LoRa-Konfiguration setzen (400..404)</h4>
  <div class="row">
    <label>Frequenz (MHz): <input id="freq_mhz" type="number" step="0.1" value="868.0"></label>
    <label>Power (dBm): <input id="power_dbm" type="number" step="1" value="22"></label>
    <label>SF: 
      <select id="sf">
        <option>7</option><option>8</option><option>9</option><option>10</option><option>11</option><option>12</option>
      </select>
    </label>
    <label>BW (kHz): 
      <select id="bw_khz">
        <option>125</option><option>250</option><option>500</option>
      </select>
    </label>
    <label>CR (4/x): 
      <select id="cr_denom">
        <option>5</option><option>6</option><option>7</option><option>8</option>
      </select>
    </label>
    <label>Slaves (optional, CSV): <input id="ids" placeholder="z.B. 1,3,5"></label>
    <button 
      hx-post="/api/lora/apply"
      hx-vals='js:{"freq_mhz": parseFloat(freq_mhz.value), "power_dbm": parseInt(power_dbm.value), "sf": parseInt(sf.value), "bw_khz": parseInt(bw_khz.value), "cr_denom": parseInt(cr_denom.value), "ids": ids.value}'
      hx-target="#toast">LoRa anwenden</button>
  </div>

  <div id="toast" class="muted" style="margin-top:8px"></div>
</div>

<div class="card">
  <h3>Live-Werte</h3>
  <div id="live" hx-get="/api/read/compact" hx-trigger="load, every 2s" hx-swap="innerHTML">
    <div class="muted">Lade Live-Daten…</div>
  </div>
</div>

<div class="card">
  <h3>Datenbank</h3>
  <div id="db" class="muted">Noch nichts geladen.</div>
</div>

</body>
</html>
"""

app = Flask(__name__)

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, port=PORT, baud=BAUDRATE, slaves=",".join(map(str,SLAVE_IDS)))

@app.get("/api/ping")
def api_ping():
    ok = ensure_connected()
    return f'<div id="ping">Status: <b>{"verbunden" if ok else "getrennt"}</b></div>'

@app.get("/api/read/compact")
def api_read_compact():
    html = render_live_table_from_cache()
    return no_cache_html(html)

@app.get("/api/full/<int:slave_id>")
def api_full(slave_id: int):
    row = read_full_state(slave_id)
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
    data = _get_payload()
    try:
        slave_id = int(data.get("slave_id"))
    except Exception:
        return "Bad payload (slave_id fehlt)", 400
    ok = run_measure_cycle_for(slave_id, wait_ms=DEFAULT_WAIT_MS)
    live_html = render_live_table_from_cache()
    toast = f"✅ Gesendet von {slave_id} & Live aktualisiert" if ok else f"⚠️ Senden {slave_id} fehlgeschlagen"
    return no_cache_html(
        f'<div id="toast">{toast}</div>'
        f'<div id="live" hx-swap-oob="true">{live_html}</div>'
    )

@app.post("/api/live/reload")
def api_live_reload():
    live_html = render_live_table_from_cache()
    return no_cache_html(
        f'<div id="toast">🔄 Live aktualisiert</div>'
        f'<div id="live" hx-swap-oob="true">{live_html}</div>'
    )

@app.post("/api/command")
def api_command():
    data = _get_payload()
    try:
        slave_id = int(data.get("slave_id"))
        action   = str(data.get("action"))
    except Exception:
        return "Bad payload", 400
    ok = send_command(slave_id, action)
    return ("✅ OK" if ok else "❌ Modbus write failed"), (200 if ok else 502)

@app.post("/api/collect")
def api_collect():
    res = collect_and_store_all()
    return f"💾 Saved {res['saved']}/{res['total']} Datensätze."

@app.post("/api/broadcast")
def api_broadcast():
    data = _get_payload()
    action = (data.get("action") or "").strip()
    ids = data.get("ids") or SLAVE_IDS
    if isinstance(ids, str):
        ids = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    else:
        ids = [int(x) for x in ids]

    mapping = {"led_on":1, "led_off":2, "reset":99}
    val = mapping.get(action)
    if val is None:
        return "Bad action", 400

    ok = 0
    # Halte das Lock für die GESAMTE Broadcast-Sequenz -> keine Poller-Reads dazwischen
    with _client_lock:
        for sid in ids:
            if _write_reg_retry(CMD_REGISTER, val, sid):
                ok += 1
            time.sleep(BROADCAST_GAP_S)
    return f"Broadcast {action}: {ok}/{len(ids)} ok"

@app.post("/api/lora/apply")
def api_lora_apply():
    data = _get_payload()
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
            target_ids = SLAVE_IDS
    else:
        target_ids = SLAVE_IDS

    ok = 0
    with _client_lock:
        for sid in target_ids:
            if _write_regs_retry(400, [
                int(round(freq_mhz * 10)), int(power_dbm), int(sf), int(bw_khz), int(cr_denom)
            ], sid):
                ok += 1
            time.sleep(LORA_GAP_S)

    return f"LoRa gesetzt ({ok}/{len(target_ids)} ok) — Freq={freq_mhz:.1f} MHz, Pwr={power_dbm} dBm, SF={sf}, BW={bw_khz} kHz, CR=4/{cr_denom}"

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

# ---------- Start ----------
if __name__ == "__main__":
    init_db()
    ensure_connected()
    t = threading.Thread(target=poller_loop, name="modbus-poller", daemon=True)
    t.start()
    try:
        app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False, threaded=True)
    finally:
        try: client.close()
        except: pass
