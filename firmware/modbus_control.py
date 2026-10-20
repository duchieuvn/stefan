from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
import time
import logging
from datetime import datetime

# ---------- LOGGING UNTERDRÜCKEN ----------
logging.getLogger("pymodbus").setLevel(logging.ERROR)
logging.getLogger("pymodbus.logging").setLevel(logging.ERROR)

# ---------- KONFIGURATION ----------
PORT = '/dev/ttyUSB0'  # Windows: richtiger COM-Port

BAUDRATE = 9600
CMD_REGISTER = 200
SLAVE_IDS = [10, 11, 12]  # IDs deiner Knoten (wird durch Bus-Scan ersetzt)

# ID-Register / Befehl zum Aendern der ID (siehe slave/modbus_handler.h)
OWN_ID_REG = 10
NEW_ID_REG = 11
MAC_REG_BASE = 20     # 12 ASCII-Zeichen
CMD_SET_ID = 20       # neue ID aus Reg 11 im NVS speichern + Neustart
CMD_AUTO_ID = 21      # gespeicherte ID loeschen + Neustart → Slave lost neue Zufalls-ID aus
TARGET_MAC_REG = 12   # Ziel-MAC fuer CMD 22 (12 ASCII-Zeichen, 2 pro Register)
CMD_SET_ID_BY_MAC = 22  # per Broadcast: nur das Board mit dieser MAC speichert Reg 11 + Neustart
BROADCAST_ID = 0      # alle Slaves fuehren aus, keiner antwortet

# Bus-Scan: kurzer Timeout ohne Retries, sonst dauert ein Scan ueber 1..247 Minuten
SCAN_TIMEOUT = 0.2
SCAN_FIRST, SCAN_LAST = 1, 247  # Bereich fuer den Scan beim Start; enger = schneller

# BME-Register
BME_TEMP_REG = 120   # °C ×100, int16
BME_HUM_REG  = 121   # % ×100, uint16
BME_PRESS_REG = 122  # hPa ×10, uint16

# ---------- MODBUS CLIENT ----------
client = ModbusSerialClient(
    port=PORT,
    baudrate=BAUDRATE,
    stopbits=1,
    bytesize=8,
    parity='N',
    timeout=0.35,
    retries=2,   # falls deine pymodbus-Version hier meckert: diesen Parameter entfernen
)

# ---------- HILFSFUNKTIONEN ----------
def to_int16(val):
    """Wandelt 16-Bit-Register in signed int16 um."""
    return val if val < 32768 else val - 65536

# ---------- BEFEHLE SENDEN ----------
def send_command(slave_id, cmd_val, description):
    try:
        result = client.write_register(CMD_REGISTER, cmd_val, device_id=slave_id)
    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort ({e}). Weiter mit naechstem.")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
        time.sleep(0.2)
        return
    except ModbusException as e:
        print(f"[Slave {slave_id}] Modbus-Fehler: {e}")
        time.sleep(0.2)
        return
    except Exception as e:
        print(f"[Slave {slave_id}] Unerwarteter Fehler: {e}")
        time.sleep(0.2)
        return

    if result is None or result.isError():
        print(f"[Slave {slave_id}] Fehler beim Senden: {result}")
    else:
        print(f"[Slave {slave_id}] Befehl gesendet: {description}")
    time.sleep(0.2)

def send_to_all(cmd_val, description):
    print(f"\nSende '{description}' an alle Slaves {SLAVE_IDS}...")
    ok = 0
    for sid in SLAVE_IDS:
        try:
            result = client.write_register(CMD_REGISTER, cmd_val, device_id=sid)
            if result is None or result.isError():
                print(f"[Slave {sid}] Fehler beim Senden: {result}")
            else:
                print(f"[Slave {sid}] Befehl gesendet: {description} (an Slave {sid})")
                ok += 1
        except ModbusIOException as e:
            print(f"[Slave {sid}] Keine Antwort ({e}).")
            try:
                client.close()
                client.connect()
            except Exception:
                pass
        except Exception as e:
            print(f"[Slave {sid}] Unerwarteter Fehler: {e}")
        time.sleep(0.2)
    print(f"Fertig: {ok}/{len(SLAVE_IDS)} erfolgreich")

def write_timestamp(slave_id, timestamp):
    high = (timestamp >> 16) & 0xFFFF
    low = timestamp & 0xFFFF
    base_reg = 300
    try:
        result = client.write_registers(base_reg, [high, low], device_id=slave_id)
        if result.isError():
            print(f"[Slave {slave_id}] Fehler beim Schreiben des Timestamps: {result}")
        else:
            print(f"[Slave {slave_id}] Timestamp geschrieben: {timestamp} ({high}, {low})")
    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort beim Timestamp: {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] Unerwarteter Fehler beim Timestamp: {e}")
    time.sleep(0.2)

# ---------- REGISTER LESEN ----------
def read_slave_values(slave_id):
    """Kurzansicht der LoRa-basierten Werte."""
    try:
        result = client.read_holding_registers(address=100, count=16, device_id=slave_id)
        if result.isError():
            print(f"[Slave {slave_id}] Fehler: {result}")
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
        print(f"[Slave {slave_id}] Keine Antwort: {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] Fehler: {e}")

def read_all_slave_values():
    print("\nLese Werte aller Slaves...")
    for sid in SLAVE_IDS:
        read_slave_values(sid)
        time.sleep(0.1)
    print("------------------------")

# ---------- BME-WERTE LESEN ----------
def read_bme_values(slave_id):
    """Liest Temperatur, Feuchte, Druck aus Reg 120–122 und zeigt sie an."""
    try:
        res = client.read_holding_registers(address=BME_TEMP_REG, count=3, device_id=slave_id)
        if res.isError():
            print(f"[Slave {slave_id}] Fehler beim Lesen der BME-Register: {res}")
            return

        regs = res.registers
        raw_t = to_int16(regs[0])   # int16
        raw_h = regs[1]             # uint16
        raw_p = regs[2]             # uint16

        temp = raw_t / 100.0        # °C
        hum = raw_h / 100.0         # % rF
        press = raw_p / 10.0        # hPa

        now = datetime.now().strftime('%H:%M:%S')
        print(f"   Temperatur : {temp:.2f} °C")
        print(f"   Feuchte    : {hum:.2f} % rF")
        print(f"   Druck      : {press:.2f} hPa")

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort (BME): {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] Fehler beim BME-Lesen: {e}")


def read_full_state(slave_id):
    """Detaillierter Status inkl. BME-Werte."""
    try:
        # Block 1: Eigene ID und MAC (10–31)
        res1 = client.read_holding_registers(address=10, count=22, device_id=slave_id)
        if res1.isError():
            print(f"[Slave {slave_id}] Fehler in Block 1: {res1}")
            return

        # Block 2: Empfangene Werte + BME (100–122)
        res2 = client.read_holding_registers(address=100, count=23, device_id=slave_id)
        if res2.isError():
            print(f"[Slave {slave_id}] Fehler in Block 2: {res2}")
            return

        # Block 3: Gesendeter Timestamp (300–301)
        res3 = client.read_holding_registers(address=300, count=2, device_id=slave_id)
        if res3.isError():
            print(f"[Slave {slave_id}] Fehler in Block 3: {res3}")
            return

        # Block 4: LoRa-Konfiguration (400–404)
        res4 = client.read_holding_registers(address=400, count=5, device_id=slave_id)
        if res4.isError():
            print(f"[Slave {slave_id}] Fehler in Block 4 (LoRa): {res4}")
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
        print(f"\n[Slave {slave_id}] Status um {now}")
        print(f"   Eigene ID       : {own_id}")
        print(f"   MAC (ASCII)     : {mac_ascii}")
        print(f"   RX ID           : {rx_id}")
        print(f"   Empfang Zeit    : {timestamp} ms")
        print(f"   RSSI            : {rssi/10:.1f} dBm")
        print(f"   SNR             : {snr/10:.1f} dB")
        print(f"   Sende-Zeit TX   : {tx_timestamp} sec")
        print("-" * 40)
        print(f"   LoRa-Konfiguration:")
        print(f"      Frequenz     : {freq:.1f} MHz")
        print(f"      Leistung     : {power} dBm")
        print(f"      Spreading F. : SF{sf}")
        print(f"      Bandbreite   : {bw} kHz")
        print(f"      Coding Rate  : 4/{cr}")
        print("-" * 40)

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort (Diagnose): {e}")
        try:
            client.close()
            client.connect()
        except Exception:
            pass
    except Exception as e:
        print(f"[Slave {slave_id}] Fehler: {e}")

def set_lora_config(slave_id):
    try:
        print(f"\nLoRa-Konfiguration fuer Slave {slave_id}")
        freq = int(float(input("Frequenz (MHz, z. B. 868.0): ")) * 10)
        power = int(input("Sendeleistung (dBm, z. B. 22): "))
        sf = int(input("Spreading Factor (z. B. 7–12): "))
        bw = int(input("Bandbreite (kHz, z. B. 125): "))
        cr = int(input("Coding Rate (z. B. 5 fuer 4/5): "))

        regs = [freq, power, sf, bw, cr]
        try:
            result = client.write_registers(400, regs, device_id=slave_id)
            if result.isError():
                print(f"[Slave {slave_id}] Fehler beim Schreiben der LoRa-Konfiguration.")
            else:
                print(f"[Slave {slave_id}] LoRa-Konfiguration erfolgreich gesetzt.")
        except ModbusIOException as e:
            print(f"[Slave {slave_id}] Keine Antwort bei LoRa-Konfig: {e}")
            try:
                client.close()
                client.connect()
            except Exception:
                pass
        time.sleep(0.2)

    except Exception as e:
        print(f"[Slave {slave_id}] Fehler: {e}")

# ---------- BUS-SCAN / ID ----------
def scan_bus(first=1, last=247):
    """Fragt Reg 10 (eigene ID) bei jeder Adresse ab und setzt SLAVE_IDS auf die gefundenen IDs."""
    global SLAVE_IDS
    print(f"\nScanne Slave-IDs {first}..{last} (ca. {(last - first + 1) * SCAN_TIMEOUT:.0f} s)...")

    # Eigener Client mit kurzem Timeout; der Port kann nur einmal offen sein
    client.close()
    scanner = ModbusSerialClient(
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity='N',
        timeout=SCAN_TIMEOUT,
        retries=0,
    )
    found = []
    try:
        if not scanner.connect():
            print("Scan: Port konnte nicht geoeffnet werden.")
            return found
        for sid in range(first, last + 1):
            try:
                res = scanner.read_holding_registers(address=OWN_ID_REG, count=1, device_id=sid)
            except ModbusException:
                continue
            if res is None or res.isError() or res.registers[0] != sid:
                continue

            mac = "?"
            try:
                res_mac = scanner.read_holding_registers(address=MAC_REG_BASE, count=12, device_id=sid)
                if not res_mac.isError():
                    mac = ''.join(chr(c) for c in res_mac.registers)
            except ModbusException:
                pass
            print(f"   gefunden: Slave {sid}  MAC {mac}")
            found.append((sid, mac))
    finally:
        scanner.close()
        client.connect()

    if found:
        SLAVE_IDS = [sid for sid, _ in found]
        print(f"\nVerfuegbare Slaves ({len(found)}):")
        for sid, mac in found:
            print(f"   ID {sid:>3}   MAC {mac}")
    else:
        print("Keine Slaves gefunden, SLAVE_IDS unveraendert.")
    return SLAVE_IDS if found else []

def change_slave_id(slave_id):
    """Speichert eine neue ID dauerhaft im NVS des Slaves. Gibt die neue ID zurueck (oder die alte bei Fehler)."""
    try:
        new_id = int(input(f"Neue ID fuer Slave {slave_id} (1–247, 0 = neue Zufalls-ID): "))
    except ValueError:
        print("Bitte eine gueltige Zahl eingeben")
        return slave_id
    if new_id == 0:
        # Wirkt auf alle Slaves mit dieser ID: jeder lost seine eigene neue ID aus
        # (so lassen sich auch doppelte IDs trennen, deren Antworten kollidieren)
        send_command(slave_id, CMD_AUTO_ID, "neue Zufalls-ID + Neustart")
        SLAVE_IDS[:] = [sid for sid in SLAVE_IDS if sid != slave_id]
        print("Slave(s) starten neu mit einer Zufalls-ID. Mit [s] neu scannen.")
        return slave_id
    if not 1 <= new_id <= 247 or new_id == slave_id:
        print("Ungueltige ID.")
        return slave_id
    if new_id in SLAVE_IDS:
        print(f"ID {new_id} ist bereits vergeben (letzter Scan: {SLAVE_IDS}).")
        return slave_id

    try:
        result = client.write_register(NEW_ID_REG, new_id, device_id=slave_id)
        if result is None or result.isError():
            print(f"[Slave {slave_id}] Fehler beim Schreiben der neuen ID: {result}")
            return slave_id
    except ModbusException as e:
        print(f"[Slave {slave_id}] Keine Antwort: {e}")
        return slave_id

    send_command(slave_id, CMD_SET_ID, f"ID-Wechsel auf {new_id} + Neustart")
    SLAVE_IDS[:] = [new_id if sid == slave_id else sid for sid in SLAVE_IDS]
    print(f"Slave startet neu und meldet sich als ID {new_id}.")
    return new_id

def change_id_by_mac():
    """Setzt die ID des Boards mit einer bestimmten MAC (wie auf dem OLED), auch bei doppelten IDs."""
    raw = input("MAC-Adresse (wie auf dem OLED, z. B. 48CA433A6C6C): ")
    mac = ''.join(c for c in raw if c not in ":-. ").upper()
    if len(mac) != 12 or any(c not in "0123456789ABCDEF" for c in mac):
        print("Ungueltige MAC (12 Hex-Zeichen).")
        return
    try:
        new_id = int(input("Neue ID (1–247): "))
    except ValueError:
        print("Bitte eine gueltige Zahl eingeben")
        return
    if not 1 <= new_id <= 247:
        print("Ungueltige ID.")
        return
    if new_id in SLAVE_IDS:
        print(f"ID {new_id} ist bereits vergeben (letzter Scan: {SLAVE_IDS}).")
        return

    # Broadcast: jedes Board bekommt Reg 11..17 und CMD 22, nur das mit passender MAC uebernimmt
    regs = [new_id] + [(ord(mac[i]) << 8) | ord(mac[i + 1]) for i in range(0, 12, 2)]
    try:
        client.write_registers(NEW_ID_REG, regs, device_id=BROADCAST_ID, no_response_expected=True)
        time.sleep(0.3)  # Pause, damit die Slaves die beiden Frames getrennt erkennen
        client.write_register(CMD_REGISTER, CMD_SET_ID_BY_MAC, device_id=BROADCAST_ID,
                              no_response_expected=True)
    except ModbusException as e:
        print(f"Fehler beim Senden: {e}")
        return
    print(f"An MAC {mac} gesendet: neue ID {new_id}. Warte auf Neustart...")

    # Pruefen: antwortet unter der neuen ID ein Board mit genau dieser MAC?
    deadline = time.time() + 8
    while time.time() < deadline:
        time.sleep(1)
        try:
            res = client.read_holding_registers(address=MAC_REG_BASE, count=12, device_id=new_id)
        except ModbusException:
            continue
        if res is None or res.isError():
            continue
        found = ''.join(chr(c) for c in res.registers)
        if found == mac:
            if new_id not in SLAVE_IDS:
                SLAVE_IDS.append(new_id)
            print(f"OK: Board {mac} meldet sich als ID {new_id}.")
        else:
            print(f"Unter ID {new_id} antwortet ein anderes Board ({found}).")
        return
    print(f"Keine Antwort unter ID {new_id}. MAC richtig? Board mit neuer Firmware? Mit [s] scannen.")

# ---------- MENÜ ----------
def slave_menu():
    while True:
        choice = input(f"Slave-ID eingeben (1–247), [s] Bus scannen oder [k] ID per MAC setzen {SLAVE_IDS}: ").strip().lower()
        if choice == 's':
            scan_bus()
            continue
        if choice == 'k':
            change_id_by_mac()
            continue
        try:
            slave_id = int(choice)
            if 1 <= slave_id <= 247:
                return slave_id
            else:
                print("Ungueltige ID (1–247)")
        except ValueError:
            print("Bitte eine gueltige Zahl eingeben")

def command_menu(slave_id):
    while True:
        print(f"\nAktiver Slave: {slave_id}")
        print("[1] LED AN")
        print("[2] LED AUS")
        print("[3] Neustart ESP32")
        print("[m] LoRa-Messung ausloesen (Timestamp + CMD 10)")
        print("[b] BME-Messung ausloesen + Werte lesen")
        print("[a] Alle Slaves: Befehl senden")
        print("[v] Alle Slaves: Kurz-Werte anzeigen")
        print("[f] Aktiven Slave: Vollstaendige Werte (inkl. BME)")
        print("[s] Bus scannen (verfuegbare Slave-IDs suchen)")
        print("[n] ID des aktiven Slaves dauerhaft aendern")
        print("[k] ID per MAC-Adresse aendern (auch bei doppelten IDs)")
        print("[i] Anderen Slave waehlen")
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
            print(f"\nSende Zeitstempel: {ts} ({readable})")
            write_timestamp(slave_id, ts)
            time.sleep(0.1)
            send_command(slave_id, 10, "LoRa-Messung / Broadcast (CMD 10)")
        elif choice == 'b':
            trigger_bme_measurement(slave_id, auto_read=True)
        elif choice == 'a':
            print("\nBefehl fuer alle Slaves:")
            print("[1] LED AN")
            print("[2] LED AUS")
            print("[3] Neustart ESP32")
            print("[4] LoRa-Konfiguration setzen")
            sub_choice = input("Befehl waehlen: ").strip()
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
                    cr = int(input("Coding Rate (z. B. 5 fuer 4/5): "))
                    regs = [freq, power, sf, bw, cr]
                    for sid in SLAVE_IDS:
                        try:
                            result = client.write_registers(400, regs, device_id=sid)
                            if result.isError():
                                print(f"[Slave {sid}] Fehler beim Schreiben.")
                            else:
                                print(f"[Slave {sid}] Konfiguration geschrieben.")
                        except ModbusIOException as e:
                            print(f"[Slave {sid}] Keine Antwort beim Schreiben: {e}")
                            try:
                                client.close()
                                client.connect()
                            except Exception:
                                pass
                        time.sleep(0.2)
                except Exception as e:
                    print("Fehler bei Eingabe oder Uebertragung:", e)
        elif choice == 'v':
            read_all_slave_values()
        elif choice == 'f':
            read_full_state(slave_id)
        elif choice == 's':
            scan_bus()
        elif choice == 'n':
            slave_id = change_slave_id(slave_id)
        elif choice == 'k':
            change_id_by_mac()
        elif choice == 'i':
            return 'change_id'
        elif choice == 'q':
            return 'quit'
        else:
            print("Ungueltige Eingabe.")

# ---------- HAUPTPROGRAMM ----------
if client.connect():
    print("Verbindung zum Modbus hergestellt")

    try:
        scan_bus(SCAN_FIRST, SCAN_LAST)
        slave_id = slave_menu()

        while True:
            action = command_menu(slave_id)
            if action == 'change_id':
                slave_id = slave_menu()
            elif action == 'quit':
                break
    finally:
        client.close()
        print("Verbindung geschlossen.")
else:
    print("Verbindung fehlgeschlagen.")
