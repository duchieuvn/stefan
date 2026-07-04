# interface.py  — Modbus-Interface + Poller (aus deinem monolithischen Skript extrahiert)
from __future__ import annotations
from pymodbus.client import ModbusSerialClient
import time, logging, threading, os, glob
from typing import Optional, List, Dict, Any
from pathlib import Path

log = logging.getLogger("modbus-if")
log.setLevel(logging.INFO)

# ------- Autodetect -------
def autodetect_port() -> str | None:
    for pat in ("/dev/cu.usbserial*", "/dev/tty.usbserial*", "/dev/ttyUSB*", "/dev/cu.usbmodem*"):
        m = sorted(glob.glob(pat))
        if m:
            return m[0]
    return None

# ------- Konfig aus ENV (kompatibel zu deinem Skript) -------
PORT      = os.getenv("MB_PORT", autodetect_port() or "/dev/cu.usbserial-110")
BAUDRATE  = int(os.getenv("MB_BAUDRATE", "9600"))
CMD_REGISTER = 200
SLAVE_IDS: List[int] = [int(x) for x in (os.getenv("MB_SLAVES","1,2,3,4,5,6,7")).split(",") if x.strip().isdigit()]

CLIENT_TIMEOUT_S     = float(os.getenv("MB_TIMEOUT", "0.6"))
CLIENT_RETRIES       = int(os.getenv("MB_RETRIES", "0"))
READ_GAP_S           = float(os.getenv("MB_READ_GAP", "0.07"))
POLL_CYCLE_PAUSE_S   = float(os.getenv("MB_CYCLE_PAUSE", "0.2"))
DEFAULT_WAIT_MS      = int(os.getenv("MB_WAIT_MS", "1200"))

WRITE_RETRIES        = int(os.getenv("MB_WRITE_RETRIES", "2"))
WRITE_RETRY_DELAY_S  = float(os.getenv("MB_WRITE_RETRY_DELAY", "0.08"))
BROADCAST_GAP_S      = float(os.getenv("MB_BROADCAST_GAP", "0.08"))
LORA_GAP_S           = float(os.getenv("MB_LORA_GAP", "0.08"))

def signed16(v:int)->int: return v if v<32768 else v-65536

class ModbusInterface:
    def __init__(self):
        self.port = PORT
        self.baudrate = BAUDRATE
        self.slave_ids = SLAVE_IDS[:]

        self.client = ModbusSerialClient(
            port=self.port, baudrate=self.baudrate, stopbits=1, bytesize=8, parity="N",
            timeout=CLIENT_TIMEOUT_S, retries=CLIENT_RETRIES
        )
        self._lock = threading.RLock()
        self._last_try = 0.0
        self._poller_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        # Cache
        self.latest: Dict[int, Dict[str, Any]] = {}
        self._mac_cache: Dict[int, str] = {}
        self._priority: Dict[str, Any] = {"slave": None, "until": 0.0}

    # ---------- Verbindung ----------
    def ensure_connected(self) -> bool:
        if self.client.connected:
            return True
        now = time.monotonic()
        if now - self._last_try < 1.0:
            return self.client.connected
        self._last_try = now
        try:
            ok = self.client.connect()
            if ok:
                log.info("✅ Modbus verbunden (%s @ %s baud)", self.port, self.baudrate)
            return ok
        except Exception as e:
            log.warning("connect failed: %s", e)
            return False

    # ---------- Low-level (ohne Lock) ----------
    def _write_reg_nl(self, addr: int, val: int, slave: int) -> bool:
        if not self.ensure_connected(): return False
        try:
            res = self.client.write_register(addr, val, slave=slave)
            return bool(res) and not res.isError()
        except Exception:
            try: self.client.close(); self.client.connect()
            except: pass
            try:
                res = self.client.write_register(addr, val, slave=slave)
                return bool(res) and not res.isError()
            except Exception:
                return False

    def _write_regs_nl(self, addr: int, vals: List[int], slave: int) -> bool:
        if not self.ensure_connected(): return False
        try:
            res = self.client.write_registers(addr, vals, slave=slave)
            return bool(res) and not res.isError()
        except Exception:
            try: self.client.close(); self.client.connect()
            except: pass
            try:
                res = self.client.write_registers(addr, vals, slave=slave)
                return bool(res) and not res.isError()
            except Exception:
                return False

    def _read_regs_nl(self, addr: int, count: int, slave: int) -> Optional[List[int]]:
        if not self.ensure_connected(): return None
        try:
            res = self.client.read_holding_registers(address=addr, count=count, slave=slave)
            if not res or res.isError(): return None
            return res.registers
        except Exception:
            try: self.client.close(); self.client.connect()
            except: pass
            try:
                res = self.client.read_holding_registers(address=addr, count=count, slave=slave)
                if not res or res.isError(): return None
                return res.registers
            except Exception:
                return None

    # ---------- Retry-Wrapper (Lock muss gehalten werden) ----------
    def _write_reg_retry(self, addr: int, val: int, slave: int) -> bool:
        for _ in range(1 + WRITE_RETRIES):
            if self._write_reg_nl(addr, val, slave):
                return True
            time.sleep(WRITE_RETRY_DELAY_S)
        return False

    def _write_regs_retry(self, addr: int, vals: List[int], slave: int) -> bool:
        for _ in range(1 + WRITE_RETRIES):
            if self._write_regs_nl(addr, vals, slave):
                return True
            time.sleep(WRITE_RETRY_DELAY_S)
        return False

    # ---------- Poller ----------
    def _read_compact_locked(self, slave_id:int) -> Optional[Dict[str,Any]]:
        r2 = self._read_regs_nl(100, 16, slave_id)
        if not r2: return None
        rx_id   = r2[0]
        recv_ts = (r2[12] << 16) | r2[13]
        rssi    = signed16(r2[14]) / 10.0
        snr     = signed16(r2[15]) / 10.0
        if slave_id not in self._mac_cache:
            r1 = self._read_regs_nl(10, 22, slave_id)
            self._mac_cache[slave_id] = "".join(chr(c) for c in r1[10:22]) if r1 else ""
        return {
            "slave_id": slave_id,
            "mac": self._mac_cache.get(slave_id, ""),
            "rx_id": rx_id,
            "recv_ts": recv_ts,
            "rssi": rssi,
            "snr": snr,
            "t_read": time.time(),
        }

    def _poller_loop(self):
        log.info("🌀 Poller gestartet.")
        while not self._stop_evt.is_set():
            try:
                self.ensure_connected()
                ids = self.slave_ids[:]
                now = time.monotonic()
                prio = self._priority.get("slave")
                prio_until = self._priority.get("until", 0.0)
                if prio and now < prio_until:
                    ids = [prio] + [i for i in ids if i != prio]
                else:
                    self._priority["slave"] = None
                    self._priority["until"] = 0.0

                for sid in ids:
                    with self._lock:
                        data = self._read_compact_locked(sid)
                        if data:
                            self.latest[sid] = data
                    time.sleep(READ_GAP_S)
                time.sleep(POLL_CYCLE_PAUSE_S)
            except Exception as e:
                log.warning("Poller-Fehler: %s", e)
                time.sleep(0.5)

    def start_poller(self):
        if self._poller_thread and self._poller_thread.is_alive():
            return
        self._stop_evt.clear()
        self._poller_thread = threading.Thread(target=self._poller_loop, name="modbus-poller", daemon=True)
        self._poller_thread.start()

    def stop(self):
        self._stop_evt.set()
        try:
            if self._poller_thread: self._poller_thread.join(timeout=2.0)
        except: pass
        try: self.client.close()
        except: pass

    # ---------- High-level Ops ----------
    def write_timestamp(self, slave_id:int, ts:int)->bool:
        hi, lo = (ts>>16)&0xFFFF, ts&0xFFFF
        with self._lock:
            return self._write_regs_retry(300, [hi,lo], slave_id)

    def send_command(self, slave_id:int, action:str)->bool:
        mapping = {"led_on":1, "led_off":2, "reset":99}
        val = mapping.get(action)
        if val is None: return False
        with self._lock:
            return self._write_reg_retry(CMD_REGISTER, val, slave_id)

    def broadcast(self, action:str, ids:List[int]) -> int:
        mapping = {"led_on":1, "led_off":2, "reset":99}
        val = mapping.get(action)
        if val is None: return 0
        ok = 0
        with self._lock:
            for sid in ids:
                if self._write_reg_retry(CMD_REGISTER, val, sid):
                    ok += 1
                time.sleep(BROADCAST_GAP_S)
        return ok

    def write_lora_config_for(self, slave_id:int, *, freq_mhz:float, power_dbm:int, sf:int, bw_khz:int, cr_denom:int)->bool:
        if not (860.0 <= freq_mhz <= 930.0): return False
        if not (0 <= power_dbm <= 22): return False
        if sf not in (7,8,9,10,11,12): return False
        if bw_khz not in (125,250,500): return False
        if cr_denom not in (5,6,7,8): return False
        regs = [int(round(freq_mhz*10)), int(power_dbm), int(sf), int(bw_khz), int(cr_denom)]
        with self._lock:
            return self._write_regs_retry(400, regs, slave_id)

    def read_full_state(self, slave_id:int)->Optional[Dict[str,Any]]:
        with self._lock:
            r1=self._read_regs_nl(10,22,slave_id)
            r2=self._read_regs_nl(100,16,slave_id)
            r3=self._read_regs_nl(300,2,slave_id)
            r4=self._read_regs_nl(400,5,slave_id)
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

    def run_measure_cycle_for(self, slave_id:int, wait_ms:int = DEFAULT_WAIT_MS)->bool:
        ts = int(time.time())
        ts_ok = self.write_timestamp(slave_id, ts)
        with self._lock:
            meas_ok = self._write_reg_retry(CMD_REGISTER, 10, slave_id)  # 10 = measure
        self._priority["slave"] = slave_id
        self._priority["until"] = time.monotonic() + max(wait_ms/1000.0, 0.8)
        time.sleep(max(wait_ms, 0)/1000.0)
        return bool(ts_ok and meas_ok)


    # ---- Health/Diag ----
    def is_poller_running(self) -> bool:
        return bool(self._poller_thread and self._poller_thread.is_alive())

    def diag_probe(self, slaves: List[int] | None = None) -> dict:
        """Einmal synchron pro Slave Reg100 lesen. Zählt Erfolge/Fehlschläge."""
        slaves = slaves or self.slave_ids
        ok = 0; fail = 0; details = []
        with self._lock:
            for sid in slaves:
                regs = self._read_regs_nl(100, 1, sid)
                if regs is None:
                    fail += 1; details.append((sid, "read_fail"))
                else:
                    ok += 1; details.append((sid, "ok"))
                time.sleep(0.06)
        return {"ok": ok, "fail": fail, "details": details, "connected": self.client.connected}

    def tick_once(self) -> int:
        """Einen Poll-Durchlauf erzwingen (hilfreich zum Start)."""
        count = 0
        with self._lock:
            for sid in self.slave_ids:
                data = self._read_compact_locked(sid)
                if data:
                    self.latest[sid] = data
                    count += 1
                time.sleep(READ_GAP_S)
        return count
