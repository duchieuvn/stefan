
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
import time
import logging
from datetime import datetime
import sqlite3
from pathlib import Path

# ---------- LOGGING UNTERDRÜCKEN ----------
logging.getLogger("pymodbus").setLevel(logging.ERROR)
logging.getLogger("pymodbus.logging").setLevel(logging.ERROR)

# ---------- KONFIGURATION ----------
PORT = '/dev/tty.usbserial-110'  # Anpassen!
BAUDRATE = 9600
CMD_REGISTER = 200
SLAVE_IDS = [1, 2, 3, 4, 5, 6, 7]

# DB
DB_PATH = Path("modbus_data.db")

# ---------- MODBUS CLIENT ----------
client = ModbusSerialClient(
    port=PORT,
    baudrate=BAUDRATE,
    stopbits=1,
    bytesize=8,
    parity='N',
    timeout=0.35,
    retries=2
)

# ---------- DB HILFSFUNKTIONEN ----------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # nur noch: vollständiger Zustand (entspricht read_full_state / "f")
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
    )
    """)
    con.commit()
    con.close()

def save_full_state(row):
    """ row: dict mit allen Feldern aus read_full_state_parsed """
    if not row:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO full_state (
            slave_id, own_id, mac_ascii, rx_id, recv_ts, rssi, snr, tx_ts, freq, power, sf, bw, cr
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["slave_id"], row["own_id"], row["mac_ascii"], row["rx_id"], row["recv_ts"],
        row["rssi"], row["snr"], row["tx_ts"], row["freq"], row["power"], row["sf"], row["bw"], row["cr"]
    ))
    con.commit()
    con.close()

def _print_table(headers, rows):
    """Kleine ASCII-Tabelle ohne externe Libs."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))
    def fmt_row(row):
        return " | ".join(str(v).ljust(widths[i]) for i, v in enumerate(row))
    sep = "-+-".join("-" * w for w in widths)
    print(fmt_row(headers))
    print(sep)
    for r in rows:
        print(fmt_row(r))

def show_last_fullstate(limit=21):
    """Letzte N Zeilen aus full_state anzeigen (neueste zuerst)."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT id, slave_id, own_id, mac_ascii, rx_id, recv_ts, rssi, snr,
               tx_ts, freq, power, sf, bw, cr, read_at
        FROM full_state
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    con.close()
    print(f"\n📚 Letzte {limit} Einträge aus full_state (neueste zuerst):")
    if not rows:
        print("— keine Daten vorhanden —")
        return
    headers = ["id","slave_id","own_id","mac_ascii","rx_id","recv_ts","rssi","snr",
               "tx_ts","freq","power","sf","bw","cr","read_at"]
    _print_table(headers, rows)


# ---------- BEFEHLE SENDEN ----------
def send_command(slave_id, cmd_val, description):
    try:
        result = client.write_register(CMD_REGISTER, cmd_val, slave=slave_id)
    except ModbusIOException as e:
        print(f"⚠️ [Slave {slave_id}] Keine Antwort ({e}). Weiter mit nächstem.")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
        time.sleep(0.2)
        return
    except ModbusException as e:
        print(f"❌ [Slave {slave_id}] Modbus-Fehler: {e}")
        time.sleep(0.2)
        return
    except Exception as e:
        print(f"❌ [Slave {slave_id}] Unerwarteter Fehler: {e}")
        time.sleep(0.2)
        return

    if result is None or result.isError():
        print(f"❌ [Slave {slave_id}] Fehler beim Senden: {result}")
    else:
        print(f"✅ [Slave {slave_id}] Befehl gesendet: {description}")
    time.sleep(0.2)

def send_to_all(cmd_val, description):
    print(f"\n🌐 Sende '{description}' an alle Slaves {SLAVE_IDS}...")
    ok = 0
    for sid in SLAVE_IDS:
        try:
            result = client.write_register(CMD_REGISTER, cmd_val, slave=sid)
            if result is None or result.isError():
                print(f"❌ [Slave {sid}] Fehler beim Senden: {result}")
            else:
                print(f"✅ [Slave {sid}] Befehl gesendet: {description} (an Slave {sid})")
                ok += 1
        except ModbusIOException as e:
            print(f"⚠️ [Slave {sid}] Keine Antwort ({e}).")
            try:
                client.close()
                client.connect()
            except Exception:
                pass
        except Exception as e:
            print(f"❌ [Slave {sid}] Unerwarteter Fehler: {e}")
        time.sleep(0.2)
    print(f"— Fertig: {ok}/{len(SLAVE_IDS)} erfolgreich —")

def write_timestamp(slave_id, timestamp):
    high = (timestamp >> 16) & 0xFFFF
    low = timestamp & 0xFFFF
    base_reg = 300
    try:
        result = client.write_registers(base_reg, [high, low], slave=slave_id)
        if result.isError():
            print(f"❌ [Slave {slave_id}] Fehler beim Schreiben des Timestamps: {result}")
        else:
            print(f"⏱️ [Slave {slave_id}] Timestamp geschrieben: {timestamp} ({high}, {low})")
    except ModbusIOException as e:
        print(f"⚠️ [Slave {slave_id}] Keine Antwort beim Timestamp: {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"❌ [Slave {slave_id}] Unerwarteter Fehler beim Timestamp: {e}")
    time.sleep(0.2)

# ---------- REGISTER LESEN (PARSED) ----------
def _to_signed(v):
    return v if v < 32768 else v - 65536

def read_slave_values_parsed(slave_id):
    """
    Liest Block ab Adresse 100 (count=16).
    Nur zur Anzeige verwendet – wird NICHT gespeichert.
    """
    try:
        result = client.read_holding_registers(address=100, count=16, slave=slave_id)
        if result.isError():
            print(f"[Slave {slave_id}] ❌ Fehler: {result}")
            return None
        regs = result.registers
        mac = ''.join(chr(c) for c in regs[0:12])
        timestamp = (regs[12] << 16) | regs[13]
        rssi = _to_signed(regs[14]) / 10.0
        snr  = _to_signed(regs[15]) / 10.0
        return {
            "slave_id": slave_id,
            "mac": mac,
            "device_timestamp": timestamp,
            "rssi": rssi,
            "snr": snr
        }
    except ModbusIOException as e:
        print(f"[Slave {slave_id}] ⚠️ Keine Antwort: {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[Slave {slave_id}] ⚠️ Fehler: {e}")
        return None

def read_full_state_parsed(slave_id):
    """Liest alle 4 Blöcke (10–31, 100–115, 300–301, 400–404). Gibt dict zurück oder None."""
    try:
        res1 = client.read_holding_registers(address=10, count=22, slave=slave_id)
        if res1.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 1: {res1}")
            return None

        res2 = client.read_holding_registers(address=100, count=16, slave=slave_id)
        if res2.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 2: {res2}")
            return None

        res3 = client.read_holding_registers(address=300, count=2, slave=slave_id)
        if res3.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 3: {res3}")
            return None

        res4 = client.read_holding_registers(address=400, count=5, slave=slave_id)
        if res4.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 4 (LoRa): {res4}")
            return None

        regs1 = res1.registers
        regs2 = res2.registers
        regs3 = res3.registers
        regs4 = res4.registers

        own_id = regs1[0]
        mac_ascii = ''.join(chr(c) for c in regs1[10:22])

        rx_id = regs2[0]
        recv_ts = (regs2[12] << 16) | regs2[13]
        rssi = _to_signed(regs2[14]) / 10.0
        snr  = _to_signed(regs2[15]) / 10.0

        tx_ts = (regs3[0] << 16) | regs3[1]

        freq = regs4[0] / 10.0
        power = regs4[1]
        sf = regs4[2]
        bw = regs4[3]
        cr = regs4[4]

        return {
            "slave_id": slave_id,
            "own_id": own_id,
            "mac_ascii": mac_ascii,
            "rx_id": rx_id,
            "recv_ts": recv_ts,
            "rssi": rssi,
            "snr": snr,
            "tx_ts": tx_ts,
            "freq": freq,
            "power": power,
            "sf": sf,
            "bw": bw,
            "cr": cr
        }

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] ⚠️ Keine Antwort (Diagnose): {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[Slave {slave_id}] ⚠️ Fehler: {e}")
        return None

# ---------- REGISTER LESEN (PRINT wie bisher) ----------
def read_slave_values(slave_id):
    data = read_slave_values_parsed(slave_id)
    if data:
        now = datetime.now().strftime('%H:%M:%S')
        print(f"[{now}] [Slave {slave_id}] MAC: {data['mac']} | RSSI: {data['rssi']:.1f} dBm | SNR: {data['snr']:.1f} dB | Zeit: {data['device_timestamp']} ms")

def read_all_slave_values():
    print("\n📊 Lese Werte aller Slaves...")
    for sid in SLAVE_IDS:
        read_slave_values(sid)  # nur Anzeige, KEIN DB-Write
        time.sleep(0.1)
    print("------------------------")

def read_full_state(slave_id):
    row = read_full_state_parsed(slave_id)
    if row:
        now = datetime.now().strftime('%H:%M:%S')
        print(f"\n🛰️ [Slave {slave_id}] Status um {now}")
        print(f"🆔 Eigene ID     : {row['own_id']}")
        print(f"🔢 MAC (ASCII)   : {row['mac_ascii']}")
        print(f"📶 RX ID         : {row['rx_id']}")
        print(f"⏱️ Empfang Zeit  : {row['recv_ts']} ms")
        print(f"📡 RSSI          : {row['rssi']:.1f} dBm")
        print(f"🎯 SNR           : {row['snr']:.1f} dB")
        print(f"⏲️ Sende-Zeit TX : {row['tx_ts']} sec")
        print("-" * 40)
        print(f"📡 LoRa-Konfiguration:")
        print(f"   📻 Frequenz      : {row['freq']:.1f} MHz")
        print(f"   🔋 Leistung      : {row['power']} dBm")
        print(f"   🔀 Spreading F.  : SF{row['sf']}")
        print(f"   🧬 Bandbreite    : {row['bw']} kHz")
        print(f"   🧾 Coding Rate   : 4/{row['cr']}")
        print("-" * 40)

# ---------- SAMMELN & SPEICHERN (nur full_state) ----------
def collect_and_store_all():
    """
    Für alle Slaves:
      - vollständigen Zustand lesen (wie 'f') und in full_state speichern
    """
    print("\n💾 Lese & speichere FULL-STATE aller Slaves in DB...")
    ok_full = 0
    for sid in SLAVE_IDS:
        fs = read_full_state_parsed(sid)
        if fs:
            save_full_state(fs)
            print(f"✅ [Slave {sid}] full_state gespeichert")
            ok_full += 1
        time.sleep(0.1)

    print(f"— Fertig: full_state {ok_full}/{len(SLAVE_IDS)} —")
    print(f"📁 Datenbank: {DB_PATH.resolve()}")

# ---------- MENÜ ----------
def slave_menu():
    while True:
        try:
            slave_id = int(input("🔢 Neue Slave-ID eingeben (1–254): "))
            if 1 <= slave_id <= 254:
                return slave_id
            else:
                print("⚠️ Ungültige ID (1–254)")
        except ValueError:
            print("⚠️ Bitte eine gültige Zahl eingeben")

def command_menu(slave_id):
    while True:
        print(f"\n📡 Aktiver Slave: {slave_id}")
        print("[1] LED AN")
        print("[2] LED AUS")
        print("[3] Neustart ESP32")
        print("[m] Messung auslösen")
        print("[a] Alle Slaves: Befehl senden")
        print("[v] Alle Slaves: Werte anzeigen (kompakt, kein DB-Write)")
        print("[f] Aktiven Slave: Alle Register lesen (Diagnose)")
        print("[s] Alle Slaves lesen & in DB speichern (nur FULL-STATE)")  # angepasst
        print("[d] DB: letzte 21 Full-State anzeigen")
        print("[i] Slave-ID ändern")
        print("[q] Beenden")

        choice = input("Eingabe: ").strip().lower()

        if choice == '1':
            send_command(slave_id, 1, "LED AN")
        elif choice == '2':
            send_command(slave_id, 2, "LED AUS")
        elif choice == '3':
            send_command(slave_id, 99, "ESP32 Neustart")
        elif choice == 'm':
            ts = int(time.time())
            readable = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n⏱️ Sende Zeitstempel: {ts} ({readable})")
            write_timestamp(slave_id, ts)
            time.sleep(0.1)
            send_command(slave_id, 10, "Messung auslösen")
        elif choice == 'a':
            print("\nBefehl für alle Slaves:")
            print("[1] LED AN")
            print("[2] LED AUS")
            print("[3] Neustart ESP32")
            print("[4] LoRa-Konfiguration setzen")
            sub_choice = input("Befehl wählen: ").strip()
            if sub_choice == '1':
                send_to_all(1, "LED AN")
            elif sub_choice == '2':
                send_to_all(2, "LED AUS")
            elif sub_choice == '3':
                send_to_all(99, "ESP32 Neustart")
            elif sub_choice == '4':
                print("Achtung: LoRa-Konfiguration wird an alle Slaves gesendet.")
                print("Aktuelle Werte eingeben.")
                try:
                    freq = int(float(input("Frequenz (MHz, z. B. 868.0): ")) * 10)
                    power = int(input("Sendeleistung (dBm, z. B. 22): "))
                    sf = int(input("Spreading Factor (z. B. 7–12): "))
                    bw = int(input("Bandbreite (kHz, z. B. 125): "))
                    cr = int(input("Coding Rate (z. B. 5 für 4/5): "))
                    regs = [freq, power, sf, bw, cr]
                    for sid in SLAVE_IDS:
                        try:
                            result = client.write_registers(400, regs, slave=sid)
                            if result.isError():
                                print(f"[Slave {sid}] Fehler beim Schreiben.")
                            else:
                                print(f"[Slave {sid}] Konfiguration geschrieben.")
                        except ModbusIOException as e:
                            print(f"[Slave {sid}] ⚠️ Keine Antwort beim Schreiben: {e}")
                            try:
                                client.close()
                                client.connect()
                            except Exception:
                                pass
                        time.sleep(0.2)
                except Exception as e:
                    print("Fehler bei Eingabe oder Übertragung:", e)
        elif choice == 'v':
            read_all_slave_values()  # nur Anzeige
        elif choice == 'f':
            read_full_state(slave_id)  # Anzeige
        elif choice == 's':  # nur FULL-STATE speichern
            collect_and_store_all()
        elif choice == 'd':
            show_last_fullstate(21)
        elif choice == 'i':
            return 'change_id'
        elif choice == 'q':
            return 'quit'
        else:
            print("Ungültige Eingabe.")

# ---------- HAUPTPROGRAMM ----------
init_db()

if client.connect():
    print("✅ Verbindung zum Modbus hergestellt")
    print(f"📁 Datenbank: {DB_PATH.resolve()}")

    slave_id = slave_menu()

    try:
        while True:
            action = command_menu(slave_id)
            if action == 'change_id':
                slave_id = slave_menu()
            elif action == 'quit':
                break
    finally:
        client.close()
        print("🔌 Verbindung geschlossen.")
else:
    print("❌ Verbindung fehlgeschlagen.")

