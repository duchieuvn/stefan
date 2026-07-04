from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
import time
import logging
from datetime import datetime

# ---------- LOGGING UNTERDRÜCKEN ----------
logging.getLogger("pymodbus").setLevel(logging.ERROR)
logging.getLogger("pymodbus.logging").setLevel(logging.ERROR)

# ---------- KONFIGURATION ----------
PORT = '/dev/tty.usbserial-A5069RR4'  # Anpassen!
BAUDRATE = 9600
CMD_REGISTER = 200
SLAVE_IDS = [1, 2]  # IDs deiner Knoten

# BME-Register
BME_TEMP_REG = 120   # °C ×100, int16
BME_HUM_REG = 121    # % ×100, uint16
BME_PRESS_REG = 122  # hPa ×10, uint16

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

# ---------- HILFSFUNKTIONEN ----------
def to_int16(val):
    """Wandelt 16-Bit-Register in signed int16 um."""
    return val if val < 32768 else val - 65536

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

# ---------- REGISTER LESEN ----------
def read_slave_values(slave_id):
    """Kurzansicht der LoRa-basierten Werte (wie vorher)."""
    try:
        result = client.read_holding_registers(address=100, count=16, slave=slave_id)
        if result.isError():
            print(f"[Slave {slave_id}] ❌ Fehler: {result}")
            return

        regs = result.registers
        rx_id = regs[0]
        timestamp = (regs[12] << 16) | regs[13]
        rssi = to_int16(regs[14])
        snr = to_int16(regs[15])

        now = datetime.now().strftime('%H:%M:%S')
        print(f"[{now}] [Slave {slave_id}] RX-ID: {rx_id} | RSSI: {rssi/10:.1f} dBm | "
              f"SNR: {snr/10:.1f} dB | Zeit: {timestamp} ms")

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] ⚠️ Keine Antwort: {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] ⚠️ Fehler: {e}")

def read_all_slave_values():
    print("\n📊 Lese Werte aller Slaves...")
    for sid in SLAVE_IDS:
        read_slave_values(sid)
        time.sleep(0.1)
    print("------------------------")

# ---------- BME-WERTE LESEN ----------
def read_bme_values(slave_id):
    """Liest Temperatur, Feuchte, Druck aus Reg 120–122 und zeigt sie an."""
    try:
        res = client.read_holding_registers(address=BME_TEMP_REG, count=3, slave=slave_id)
        if res.isError():
            print(f"[Slave {slave_id}] ❌ Fehler beim Lesen der BME-Register: {res}")
            return

        regs = res.registers
        raw_t = to_int16(regs[0])   # int16
        raw_h = regs[1]             # uint16
        raw_p = regs[2]             # uint16

        temp = raw_t / 100.0        # °C
        hum = raw_h / 100.0         # %
        press = raw_p / 10.0        # hPa

        now = datetime.now().strftime('%H:%M:%S')
        print(f"\n🌡️ [Slave {slave_id}] BME280-Werte um {now}:")
        print(f"   Temperatur : {temp:.2f} °C")
        print(f"   Feuchte    : {hum:.2f} % rF")
        print(f"   Druck      : {press:.2f} hPa")

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] ⚠️ Keine Antwort (BME): {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] ⚠️ Fehler beim BME-Lesen: {e}")

# ---------- BME-MESSUNG AUSLÖSEN + LESEN ----------
def trigger_bme_measurement(slave_id, auto_read=True):
    """
    Löst auf dem Slave eine BME280-Messung aus (CMD=11)
    und liest optional direkt danach die BME-Register.
    """
    print(f"\n🧪 BME-Messung bei Slave {slave_id} auslösen...")
    send_command(slave_id, 11, "BME-Messung (CMD 11)")
    if auto_read:
        time.sleep(0.5)  # kurze Zeit für Messung + Register-Update
        read_bme_values(slave_id)

def read_full_state(slave_id):
    """Detaillierter Status inkl. BME-Werte."""
    try:
        # Block 1: Eigene ID und MAC (10–31)
        res1 = client.read_holding_registers(address=10, count=22, slave=slave_id)
        if res1.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 1: {res1}")
            return

        # Block 2: Empfangene Werte + BME (100–122)
        res2 = client.read_holding_registers(address=100, count=23, slave=slave_id)
        if res2.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 2: {res2}")
            return

        # Block 3: Gesendeter Timestamp (300–301)
        res3 = client.read_holding_registers(address=300, count=2, slave=slave_id)
        if res3.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 3: {res3}")
            return

        # Block 4: LoRa-Konfiguration (400–404)
        res4 = client.read_holding_registers(address=400, count=5, slave=slave_id)
        if res4.isError():
            print(f"[Slave {slave_id}] ❌ Fehler in Block 4 (LoRa): {res4}")
            return

        regs1 = res1.registers
        regs2 = res2.registers
        regs3 = res3.registers
        regs4 = res4.registers

        # Eigene ID + MAC
        own_id = regs1[0]
        mac_ascii = ''.join(chr(c) for c in regs1[10:22])  # 20–31

        # Empfangene LoRa-Daten
        rx_id = regs2[0]                           # Reg 100
        timestamp = (regs2[12] << 16) | regs2[13] # 112/113
        rssi = to_int16(regs2[14])                # 114
        snr = to_int16(regs2[15])                 # 115

        # BME-Werte (Reg 120–122 → Index 20–22)
        raw_t = to_int16(regs2[20])
        raw_h = regs2[21]
        raw_p = regs2[22]
        temp = raw_t / 100.0
        hum = raw_h / 100.0
        press = raw_p / 10.0

        # TX-Timestamp
        tx_timestamp = (regs3[0] << 16) | regs3[1]

        # LoRa-Konfig
        freq = regs4[0] / 10.0   # MHz
        power = regs4[1]         # dBm
        sf = regs4[2]
        bw = regs4[3]            # kHz
        cr = regs4[4]            # 5 → 4/5

        now = datetime.now().strftime('%H:%M:%S')
        print(f"\n🛰️ [Slave {slave_id}] Status um {now}")
        print(f"🆔 Eigene ID       : {own_id}")
        print(f"🔢 MAC (ASCII)     : {mac_ascii}")
        print(f"📶 RX ID           : {rx_id}")
        print(f"⏱️ Empfang Zeit    : {timestamp} ms")
        print(f"📡 RSSI            : {rssi/10:.1f} dBm")
        print(f"🎯 SNR             : {snr/10:.1f} dB")
        print(f"⏲️ Sende-Zeit TX   : {tx_timestamp} sec")
        print("-" * 40)
        print(f"🌡️ BME280:")
        print(f"   Temperatur      : {temp:.2f} °C")
        print(f"   Feuchte         : {hum:.2f} % rF")
        print(f"   Druck           : {press:.2f} hPa")
        print("-" * 40)
        print(f"📡 LoRa-Konfiguration:")
        print(f"   📻 Frequenz      : {freq:.1f} MHz")
        print(f"   🔋 Leistung      : {power} dBm")
        print(f"   🔀 Spreading F.  : SF{sf}")
        print(f"   🧬 Bandbreite    : {bw} kHz")
        print(f"   🧾 Coding Rate   : 4/{cr}")
        print("-" * 40)

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] ⚠️ Keine Antwort (Diagnose): {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] ⚠️ Fehler: {e}")

def set_lora_config(slave_id):
    try:
        print(f"\nLoRa-Konfiguration für Slave {slave_id}")
        freq = int(float(input("Frequenz (MHz, z. B. 868.0): ")) * 10)
        power = int(input("Sendeleistung (dBm, z. B. 22): "))
        sf = int(input("Spreading Factor (z. B. 7–12): "))
        bw = int(input("Bandbreite (kHz, z. B. 125): "))
        cr = int(input("Coding Rate (z. B. 5 für 4/5): "))

        regs = [freq, power, sf, bw, cr]
        try:
            result = client.write_registers(400, regs, slave=slave_id)
            if result.isError():
                print(f"[Slave {slave_id}] Fehler beim Schreiben der LoRa-Konfiguration.")
            else:
                print(f"[Slave {slave_id}] LoRa-Konfiguration erfolgreich gesetzt.")
        except ModbusIOException as e:
            print(f"[Slave {slave_id}] ⚠️ Keine Antwort bei LoRa-Konfig: {e}")
            try:
                client.close()
                client.connect()
            except Exception:
                pass
        time.sleep(0.2)

    except Exception as e:
        print(f"[Slave {slave_id}] Fehler: {e}")

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
        print("[m] LoRa-Messung auslösen (Timestamp + CMD 10)")
        print("[b] BME-Messung auslösen + Werte lesen")   # << geändert
        print("[a] Alle Slaves: Befehl senden")
        print("[v] Alle Slaves: Kurz-Werte anzeigen")
        print("[f] Aktiven Slave: Vollständige Werte (inkl. BME)")
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
            send_command(slave_id, 10, "LoRa-Messung / Broadcast (CMD 10)")
        elif choice == 'b':
            # jetzt: Messung auslösen + direkt lesen
            trigger_bme_measurement(slave_id, auto_read=True)
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
            read_all_slave_values()
        elif choice == 'f':
            read_full_state(slave_id)
        elif choice == 'i':
            return 'change_id'
        elif choice == 'q':
            return 'quit'
        else:
            print("Ungültige Eingabe.")

# ---------- HAUPTPROGRAMM ----------
if client.connect():
    print("✅ Verbindung zum Modbus hergestellt")

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
