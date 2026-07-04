import time
import threading
import logging
from pymodbus.client import ModbusSerialClient

log = logging.getLogger(__name__)
logging.getLogger("pymodbus").setLevel(logging.ERROR)

# ---------- KONFIG ----------
PORT = '/dev/tty.usbserial-110'
BAUDRATE = 9600

CENTER_ID = 7
OUTER_IDS = [1, 2, 3, 4, 5, 6]

CMD_REGISTER   = 200
TS_BASE_REG    = 300
MEASURE_WAIT_S = 1
CYCLE_PAUSE_S  = 2
PRESENCE_TTL_S = 10

# ---------- Shared State + Lock ----------
connections = {}      # sender -> receiver -> {rssi, snr, time}
presence = {}         # id -> {status, last_seen}
ACTIVE_READING  = None
LAST_TRIGGER_TS = 0

_state_lock = threading.RLock()

# ---------- Client ----------
client = ModbusSerialClient(
    port=PORT, baudrate=BAUDRATE, stopbits=1, bytesize=8, parity='N',
    timeout=0.35, retries=2,
)

# ---------- Utils ----------
def _sign16(v: int) -> int:
    return v if v < 32768 else v - 65536

def _now_unix() -> int:
    return int(time.time())

def _now_mono() -> float:
    return time.monotonic()

def _mark_online(node_id: str, now_unix: int):
    with _state_lock:
        presence[node_id] = {"status": "online", "last_seen": now_unix}

# ---------- LED-Blink ----------
LED_ON_CMD  = 1
LED_OFF_CMD = 2
_last_blink_mono = {}  # node_id -> monotonic seconds

def _do_blink(slave_id: int, on_ms: int, times: int, gap_ms: int):
    for i in range(times):
        if not _write_single(CMD_REGISTER, LED_ON_CMD, slave_id):
            break
        time.sleep(on_ms / 1000.0)
        _write_single(CMD_REGISTER, LED_OFF_CMD, slave_id)
        if i < times - 1:
            time.sleep(gap_ms / 1000.0)

def blink_led_async(slave_id: int, *, kind: str):
    now = _now_mono()
    if now - _last_blink_mono.get(slave_id, -1e9) < 0.25:
        return
    _last_blink_mono[slave_id] = now
    params = (dict(on_ms=150, times=2, gap_ms=120) if kind == 'tx'
              else dict(on_ms=80, times=1, gap_ms=0))
    threading.Thread(target=_do_blink, args=(slave_id,), kwargs=params, daemon=True).start()

# ---------- Reconnect-Wrapper ----------
def _ensure_connected() -> bool:
    if client.connected:
        return True
    try:
        return client.connect()
    except Exception as e:
        log.debug("connect failed: %s", e)
        return False

def _write_regs(addr: int, values, slave: int) -> bool:
    if not _ensure_connected():
        return False
    try:
        res = client.write_registers(addr, values, slave=slave)
        return bool(res) and not res.isError()
    except Exception:
        try:
            client.close()
        except:
            pass
        return _retry_write_regs(addr, values, slave)

def _retry_write_regs(addr: int, values, slave: int) -> bool:
    if not _ensure_connected():
        return False
    try:
        res = client.write_registers(addr, values, slave=slave)
        return bool(res) and not res.isError()
    except Exception:
        return False

def _write_single(addr: int, value: int, slave: int) -> bool:
    if not _ensure_connected():
        return False
    try:
        res = client.write_register(addr, value, slave=slave)
        return bool(res) and not res.isError()
    except Exception:
        try:
            client.close()
        except:
            pass
        if not _ensure_connected():
            return False
        try:
            res = client.write_register(addr, value, slave=slave)
            return bool(res) and not res.isError()
        except Exception:
            return False

def _read_regs(addr: int, count: int, slave: int):
    if not _ensure_connected():
        return None
    try:
        res = client.read_holding_registers(address=addr, count=count, slave=slave)
        if not res or res.isError():
            return None
        return res.registers
    except Exception:
        try:
            client.close()
        except:
            pass
        if not _ensure_connected():
            return None
        try:
            res = client.read_holding_registers(address=addr, count=count, slave=slave)
            if not res or res.isError():
                return None
            return res.registers
        except Exception:
            return None

# ---------- Modbus I/O ----------
def write_timestamp(slave_id: int, ts: int) -> bool:
    hi, lo = (ts >> 16) & 0xFFFF, ts & 0xFFFF
    return _write_regs(TS_BASE_REG, [hi, lo], slave=slave_id)

def send_command(slave_id: int, cmd_val: int) -> bool:
    return _write_single(CMD_REGISTER, cmd_val, slave=slave_id)

def read_slave_values(slave_id: int):
    regs = _read_regs(100, 16, slave_id)
    if not regs:
        return None
    rx_id = regs[0]
    ts    = (regs[12] << 16) | regs[13]
    rssi  = _sign16(regs[14]) / 10.0
    snr   = _sign16(regs[15]) / 10.0
    return {"rx_id": str(rx_id), "rssi": rssi, "snr": snr, "time": ts}

# ---------- Worker ----------
def start_modbus_worker():
    global ACTIVE_READING, LAST_TRIGGER_TS

    # Connect mit Backoff
    backoff = 1.5
    while not _ensure_connected():
        print("❌ Modbus-Verbindung fehlgeschlagen. Neuer Versuch …")
        time.sleep(min(backoff, 8.0))
        backoff *= 1.5
    print("✅ Modbus verbunden.")

    try:
        while True:
            now_unix = _now_unix()

            # 1) Messung am Center auslösen
            write_timestamp(CENTER_ID, now_unix)
            if send_command(CENTER_ID, 10):  # 10 = "messung"
                with _state_lock:
                    LAST_TRIGGER_TS = now_unix
                _mark_online(str(CENTER_ID), now_unix)
                blink_led_async(CENTER_ID, kind='tx')

            # 2) warten
            time.sleep(MEASURE_WAIT_S)

            # 3) Außenknoten lesen
            for rid in OUTER_IDS:
                with _state_lock:
                    ACTIVE_READING = str(rid)

                data = read_slave_values(rid)
                if data:
                    _mark_online(str(rid), now_unix)
                    if data.get("rx_id") == str(CENTER_ID):
                        with _state_lock:
                            c = connections.setdefault(str(CENTER_ID), {})
                            c[str(rid)] = {
                                "rssi": data["rssi"],
                                "snr":  data["snr"],
                                "time": data.get("time") or now_unix,
                            }
                        blink_led_async(rid, kind='rx')

            with _state_lock:
                ACTIVE_READING = None

                # 4) Offline nach TTL (monotonic für Intervalle nicht nötig, hier Unix ok)
                for node, info in list(presence.items()):
                    if now_unix - info.get("last_seen", 0) > PRESENCE_TTL_S:
                        info["status"] = "offline"

            # 5) Pause
            time.sleep(CYCLE_PAUSE_S)

    finally:
        try:
            client.close()
        except:
            pass

# ---------- Status-Helfer ----------
def get_status():
    with _state_lock:
        # flache Kopie als Snapshot für API
        return {
            "active": ACTIVE_READING,
            "last_trigger_ts": LAST_TRIGGER_TS,
        }

# ---------- Externe Kommandos (/command) ----------
def send_modbus_command(mac: str, action: str) -> bool:
    try:
        slave_id = int(mac)
    except ValueError:
        return False

    mapping = {"an": 1, "aus": 2, "reset": 99, "messung": 10}
    cmd_val = mapping.get(action)
    if cmd_val is None:
        return False

    return send_command(slave_id, cmd_val)
