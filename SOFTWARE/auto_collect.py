from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusIOException
import sqlite3
import time
from datetime import datetime

# ---------- KONFIGURATION ----------
PORT = "/dev/ttyUSB0"
BAUDRATE = 9600
CMD_REGISTER = 200
TIMESTAMP_BASE_REG = 300

# Liste aller Knoten im System
SLAVE_IDS = [7, 8]      # Knoten-IDs

# Register-Adressen
RX_BLOCK_ADDR = 100     # RX-ID, RSSI, SNR, Timestamp etc.
RX_BLOCK_COUNT = 16

BME_TEMP_REG  = 120     # °C ×100, int16
BME_HUM_REG   = 121     # % ×100, uint16
BME_PRESS_REG = 122     # hPa ×10, uint16

# Zeiten (bei Bedarf anpassen)
LORA_WAIT_AFTER_SEND = 3.0   # Sekunden nach LoRa-Senden, bevor gelesen wird
BME_WAIT_AFTER_TRIGGER = 1.0 # Sekunden nach BME-Ausloesen, bevor BME-Register gelesen werden
MEASURE_INTERVAL = 10        # Pause zwischen zwei kompletten Runden (alle Sender durch)


def to_int16(val):
    """Wandelt 16-Bit-Register in signed int16 um."""
    return val if val < 32768 else val - 65536


def init_db(db_path="sensor_data.db"):
    """Initialisiert die SQLite-Datenbank und legt die Tabelle an, falls noetig."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,          -- PC-Zeit (ISO)
            slave_id INTEGER, -- Empfaenger-Knoten (Modbus-ID)
            rx_id INTEGER,    -- ID des sendenden Knotens (aus RX-Block)
            rssi REAL,        -- dBm
            snr REAL,         -- dB
            bme_temp REAL,    -- °C
            bme_hum REAL,     -- % rF
            bme_press REAL    -- hPa
        )
    """)
    conn.commit()
    return conn


def send_lora_broadcast(client, sender_id):
    """
    Schreibt aktuellen UNIX-Timestamp in Reg 300/301 des sendenden Knotens
    und sendet dann CMD 10 auf CMD_REGISTER.
    Gibt den Timestamp (int) zurueck oder None bei Fehler.
    Hinweis: Der Timestamp wird vor allem zur Diagnose ausgegeben,
    die Filterlogik basiert aber auf rx_id, nicht auf node_timestamp.
    """
    ts = int(time.time())
    high = (ts >> 16) & 0xFFFF
    low = ts & 0xFFFF

    try:
        # Timestamp in den Sender schreiben
        res1 = client.write_registers(TIMESTAMP_BASE_REG, [high, low], device_id=sender_id)
        if res1.isError():
            print(f"[Knoten {sender_id}] Fehler beim Schreiben des Timestamps: {res1}")
            return None

        # CMD 10: LoRa-Messung / Broadcast
        res2 = client.write_register(CMD_REGISTER, 10, device_id=sender_id)
        if res2.isError():
            print(f"[Knoten {sender_id}] Fehler beim Senden von CMD 10: {res2}")
            return None

        readable = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[Knoten {sender_id}] LoRa-Broadcast gesendet mit Timestamp {ts} ({readable})")
        return ts

    except ModbusIOException as e:
        print(f"[Knoten {sender_id}] Keine Antwort beim LoRa-Broadcast: {e}")
        return None
    except Exception as e:
        print(f"[Knoten {sender_id}] Unerwarteter Fehler beim LoRa-Broadcast: {e}")
        return None


def trigger_bme_measurement(client, slave_id):
    """
    Loest auf einem Slave eine BME280-Messung aus (CMD 11).
    Die Firmware soll danach die Register 120–122 fuellen.
    """
    try:
        res = client.write_register(CMD_REGISTER, 11, device_id=slave_id)
        if res.isError():
            print(f"[Slave {slave_id}] Fehler beim Ausloesen der BME-Messung: {res}")
        else:
            print(f"[Slave {slave_id}] BME-Messung (CMD 11) ausgeloest.")
    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort beim BME-Ausloesen: {e}")
    except Exception as e:
        print(f"[Slave {slave_id}] Unerwarteter Fehler beim BME-Ausloesen: {e}")


def read_slave_data(client, slave_id):
    """
    Liest RX-Block (Reg 100 ff.) und BME-Register (120–122) eines Slaves.
    Gibt ein Dict mit den Werten zurück oder None bei Fehler.
    """
    try:
        # Block mit RX-ID, Timestamp, RSSI, SNR
        res_rx = client.read_holding_registers(
            address=RX_BLOCK_ADDR,
            count=RX_BLOCK_COUNT,
            device_id=slave_id
        )
        if res_rx.isError():
            print(f"[Slave {slave_id}] Fehler beim Lesen RX-Block: {res_rx}")
            return None

        regs_rx = res_rx.registers
        rx_id = regs_rx[0]                         # sendende Knoten-ID
        timestamp = (regs_rx[12] << 16) | regs_rx[13]
        rssi = to_int16(regs_rx[14]) / 10.0        # dBm
        snr = to_int16(regs_rx[15]) / 10.0         # dB

        # BME-Register
        res_bme = client.read_holding_registers(
            address=BME_TEMP_REG,
            count=3,
            device_id=slave_id
        )
        if res_bme.isError():
            print(f"[Slave {slave_id}] Fehler beim Lesen BME-Register: {res_bme}")
            return None

        regs_bme = res_bme.registers
        raw_t = to_int16(regs_bme[0])
        raw_h = regs_bme[1]
        raw_p = regs_bme[2]

        temp = raw_t / 100.0      # °C
        hum = raw_h / 100.0       # % rF
        press = raw_p / 10.0      # hPa

        return {
            "slave_id": slave_id,        # Empfaenger-Knoten
            "rx_id": rx_id,              # ID des sendenden Knotens
            "rssi": rssi,
            "snr": snr,
            "bme_temp": temp,
            "bme_hum": hum,
            "bme_press": press,
            "node_timestamp": timestamp, # Diagnose
        }

    except ModbusIOException as e:
        print(f"[Slave {slave_id}] Keine Antwort: {e}")
        return None
    except Exception as e:
        print(f"[Slave {slave_id}] Unerwarteter Fehler beim Lesen: {e}")
        return None


def store_measurement(cur, data):
    """
    Speichert ein Mess-Dict in der DB.
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    cur.execute("""
        INSERT INTO measurements (
            ts, slave_id, rx_id, rssi, snr,
            bme_temp, bme_hum, bme_press
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now_iso,
        data["slave_id"],   # Empfaenger
        data["rx_id"],      # Sender
        data["rssi"],
        data["snr"],
        data["bme_temp"],
        data["bme_hum"],
        data["bme_press"],
    ))


def main():
    # Modbus-Client
    client = ModbusSerialClient(
        port=PORT,
        baudrate=BAUDRATE,
        stopbits=1,
        bytesize=8,
        parity='N',
        timeout=0.5,
    )

    if not client.connect():
        print("Verbindung zum Modbus fehlgeschlagen")
        return

    print("Verbindung zum Modbus hergestellt")

    # Datenbank
    conn = init_db("sensor_data.db")
    cur = conn.cursor()

    try:
        while True:
            print("\n=== Neue Messrunde (alle Sender einmal dran) ===")

            # Alle Knoten nacheinander als Sender benutzen
            for sender_id in SLAVE_IDS:
                print(f"\n--- Sendephase: Knoten {sender_id} sendet ---")

                send_ts = send_lora_broadcast(client, sender_id)
                if send_ts is None:
                    print(f"[Knoten {sender_id}] LoRa-Broadcast fehlgeschlagen, ueberspringe Empfangsphase.")
                    continue

                # Wartezeit, bis LoRa empfangen und Register beschrieben sind
                time.sleep(LORA_WAIT_AFTER_SEND)

                # BME-Messung auf allen Empfaengern (also allen Knoten ausser dem Sender)
                receivers = [sid for sid in SLAVE_IDS if sid != sender_id]
                print(f"BME-Messung auf Empfaenger-Knoten {receivers} ausloesen...")
                for rid in receivers:
                    trigger_bme_measurement(client, rid)

                # Wartezeit, bis BME-Werte in den Registern stehen
                time.sleep(BME_WAIT_AFTER_TRIGGER)

                print(f"Lese alle Empfaenger nach Sendung von Knoten {sender_id}...")

                for rid in receivers:
                    data = read_slave_data(client, rid)
                    if data is None:
                        continue

                    now_str = datetime.now().strftime('%H:%M:%S')
                    print(
                        f"[{now_str}] [Empfaenger {rid}] "
                        f"RX-ID: {data['rx_id']} | "
                        f"RSSI: {data['rssi']:.1f} dBm | "
                        f"SNR: {data['snr']:.1f} dB | "
                        f"Zeit: {data['node_timestamp']} | "
                        f"T={data['bme_temp']:.2f} °C | "
                        f"rF={data['bme_hum']:.2f} % | "
                        f"p={data['bme_press']:.2f} hPa"
                    )

                    # Nur speichern, wenn der Empfaenger tatsaechlich dieses Paket
                    # von sender_id gesehen hat:
                    if data["rx_id"] != sender_id:
                        print(
                            f"[Empfaenger {rid}] RX-ID passt nicht zum Sender "
                            f"(RX-ID={data['rx_id']} != Sender {sender_id}), wird nicht gespeichert."
                        )
                        continue

                    # Optional: falls BME noch 0 ist, nicht speichern
                    if (data["bme_temp"] == 0.0 and
                        data["bme_hum"] == 0.0 and
                        data["bme_press"] == 0.0):
                        print(f"[Empfaenger {rid}] BME-Werte sind 0, Messung wird nicht gespeichert.")
                        continue

                    # In DB schreiben
                    store_measurement(cur, data)
                    conn.commit()
                    print(f"[Empfaenger {rid}] Daten gespeichert (Sender {sender_id}).")

            # Nach einer kompletten Runde ueber alle Sender:
            print(f"Warte {MEASURE_INTERVAL} Sekunden bis zur naechsten Messrunde.")
            time.sleep(MEASURE_INTERVAL)

    finally:
        conn.close()
        client.close()
        print("Verbindung und Datenbank geschlossen.")


if __name__ == "__main__":
    main()
