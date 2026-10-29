#pragma once
#include <ModbusRTU.h>
#include "radio_handler.h"
#include "heltec_led.h"
#include "ota_handler.h"
#include "device_id.h"

extern String deviceId;  // wird im Setup gesetzt (getMacFull())

// ------------------------------
// 🆔 Eigene Gerätekennung
// ------------------------------
extern uint8_t SLAVE_ID;  // aus NVS, siehe device_id.h

// Change these pins according to your TTL ebay modbus
#define TXD1 6
#define RXD1 7

// ------------------------------
// 📦 Modbus-Register-Belegung
// ------------------------------
// Die Belegung der reinen Slave-Firmware bleibt unverändert (LoRa-Logger, ID-Vergabe und OTA
// funktionieren wie bisher). Neu sind nur die CW-/Modus-Register weiter unten.

// Eigene Kennung
const uint16_t OWN_ID_REGISTER     = 10;   // eigene Slave-ID
const uint16_t NEW_ID_REGISTER     = 11;   // neue Slave-ID, wird mit CMD 20/22 im NVS gespeichert
const uint16_t TARGET_MAC_BASE     = 12;   // Ziel-MAC fuer CMD 22 (12 ASCII-Zeichen, 2 pro Register)
const uint16_t OWN_MAC_REG_BASE    = 20;   // eigene MAC (12x ASCII-Zeichen)

// Empfangene Daten über LoRa
const uint16_t RX_ID_REGISTER      = 100;  // empfangene Slave-ID
const uint16_t TIME_REG_BASE       = 112;  // 2 x 16-bit → Zeitstempel
const uint16_t RSSI_REGISTER       = 114;  // int16_t ×10
const uint16_t SNR_REGISTER        = 115;  // int16_t ×10

// 🌡️ BME280-Umweltdaten
const uint16_t TEMP_REGISTER       = 120;  // Temperatur in °C ×100 (int16_t)
const uint16_t HUM_REGISTER        = 121;  // rel. Feuchte in % ×100
const uint16_t PRESS_REGISTER      = 122;  // Luftdruck in hPa ×10

// Ergebnis der letzten RSSI-Messung (CMD 32), 130..135 am Stück lesbar
const uint16_t CW_RSSI_BLOCK_BASE  = 130;
const uint16_t CW_RSSI_AVG_REGISTER   = 130;  // int16_t ×10, Mittelwert (linear gemittelt)
const uint16_t CW_RSSI_RESERVED_REG   = 131;  // immer 0
const uint16_t CW_RSSI_MAX_REGISTER   = 132;  // int16_t ×10
const uint16_t CW_RSSI_MIN_REGISTER   = 133;  // int16_t ×10
const uint16_t CW_RSSI_COUNT_REGISTER = 134;  // Anzahl Samples
const uint16_t CW_RSSI_SEQ_REGISTER   = 135;  // +1 nach jeder fertigen Messung

// Steuern / Triggern
const uint16_t CMD_REGISTER        = 200;  // Steuerung durch Master

// Payload (optional) für LoRa-Sendung über CMD 10
const uint16_t PAYLOAD_LEN_REG    = 320;  // Länge in Bytes (0..64)
const uint16_t PAYLOAD_DATA_BASE  = 321;  // ab hier 2 ASCII-Bytes pro Register
const uint16_t PAYLOAD_MAX_BYTES  = 64;
const uint16_t PAYLOAD_WORDS      = (PAYLOAD_MAX_BYTES + 1) / 2; // 32 Words

// CMD-Konvention:
//  1  = LED an
//  2  = LED aus
// 10  = LoRa-Broadcast                       (nur im LoRa-Modus)
// 11  = BME280-Messung ausführen
// 20  = Slave-ID aus Register 11 im NVS speichern + Neustart
// 21  = gespeicherte Slave-ID loeschen + Neustart (→ neue Zufalls-ID)
// 22  = nur wenn MAC = Reg 12..17: ID aus Reg 11 speichern + Neustart (per Broadcast an alle)
// 30  = CW-Träger senden (Dauer aus CW_TX_MS_REGISTER)          (nur im CW-Modus)
// 31  = CW-Sendung / RSSI-Messung abbrechen                     (nur im CW-Modus)
// 32  = RSSI messen (Dauer aus RSSI_MEASURE_MS_REGISTER)        (nur im CW-Modus)
// 50  = OTA-Update starten (Parameter ab Register 510, siehe ota_handler.h)
// 51  = OTA-Update abbrechen
// 99  = Neustart
const uint16_t CMD_CW_START        = 30;
const uint16_t CMD_CW_STOP         = 31;
const uint16_t CMD_RSSI_MEASURE    = 32;

const uint16_t TX_TIMESTAMP_BASE   = 300;  // 2x16bit → Timestamp zum Senden

// CW-Parameter und -Zustand
const uint16_t CW_TX_MS_REGISTER        = 360;  // CW-Dauer in ms (max CW_MAX_TX_MS)
const uint16_t RSSI_MEASURE_MS_REGISTER = 361;  // Messdauer in ms (max CW_MAX_MEASURE_MS)
const uint16_t CW_STATE_REGISTER        = 362;  // nur lesen: 0 = IDLE, 1 = CW TX, 2 = Messung

// Radio-Parameter Register (gelten für beide Modi, SF/BW/CR nur im LoRa-Modus)
const uint16_t LORA_FREQ_REGISTER     = 400;  // in MHz × 10, z. B. 4330
const uint16_t LORA_POWER_REGISTER    = 401;  // in dBm
const uint16_t LORA_SF_REGISTER       = 402;  // Spreading Factor (7–12)
const uint16_t LORA_BW_REGISTER       = 403;  // Bandwidth in kHz (z. B. 125)
const uint16_t LORA_CR_REGISTER       = 404;  // Coding Rate (5 = 4/5 ... 8 = 4/8)
const uint16_t LORA_APPLY_CMD         = 410;  // 1 = übernehmen

// Betriebsart: Master schreibt den Wunsch, der Node meldet den tatsächlichen Modus
const uint16_t RADIO_MODE_REQ_REGISTER    = 420;  // 0 = LoRa, 1 = CW (nach Neustart immer 0)
const uint16_t RADIO_MODE_ACTIVE_REGISTER = 421;  // nur lesen: 0 = LoRa, 1 = CW, 0xFFFF = Radio-Init fehlgeschlagen

// ------------------------------
HardwareSerial RS485(1);
ModbusRTU mb;

uint16_t lastCmdVal = 0;

void setupModbus() {
  Serial.printf("Modbus Slave mit ID %d gestartet\n", SLAVE_ID);

  RS485.begin(9600, SERIAL_8N1, RXD1, TXD1);
  if (mb.begin(&RS485)) {
    Serial.println("✅ Modbus gestartet");
  } else {
    Serial.println("❌ Fehler beim Start von Modbus");
  }

  mb.slave(SLAVE_ID);

  // Eigene ID + MAC ablegen
  mb.addHreg(OWN_ID_REGISTER, SLAVE_ID);
  mb.addHreg(NEW_ID_REGISTER, 0);
  mb.addHreg(TARGET_MAC_BASE, 0, 6);
  for (int i = 0; i < 12; i++) {
    mb.addHreg(OWN_MAC_REG_BASE + i, deviceId[i]);
  }

  // Empfangene Daten-Register (mit Initialwert 0)
  mb.addHreg(RX_ID_REGISTER, 0);
  mb.addHreg(TIME_REG_BASE, 0);
  mb.addHreg(TIME_REG_BASE + 1, 0);
  mb.addHreg(RSSI_REGISTER, 0);
  mb.addHreg(SNR_REGISTER, 0);

  // 🌡️ BME280-Register
  mb.addHreg(TEMP_REGISTER, 0);
  mb.addHreg(HUM_REGISTER, 0);
  mb.addHreg(PRESS_REGISTER, 0);

  // RSSI-Ergebnis-Register (mit Initialwert 0)
  mb.addHreg(CW_RSSI_BLOCK_BASE, 0, 6);

  // Steuerung & Sendezeit
  mb.addHreg(CMD_REGISTER, 0);
  mb.addHreg(TX_TIMESTAMP_BASE, 0);
  mb.addHreg(TX_TIMESTAMP_BASE + 1, 0);

  // Payload Register (für id@ts@payload)
  mb.addHreg(PAYLOAD_LEN_REG, 0);
  for (int i = 0; i < PAYLOAD_WORDS; i++) {
    mb.addHreg(PAYLOAD_DATA_BASE + i, 0);
  }

  // CW-Parameter und -Zustand
  mb.addHreg(CW_TX_MS_REGISTER, 2000);
  mb.addHreg(RSSI_MEASURE_MS_REGISTER, 1000);
  mb.addHreg(CW_STATE_REGISTER, 0);

  // Radio-Konfiguration als Holding-Register ablegen – mit genau den Werten, auf die das
  // Radio beim Start wirklich konfiguriert wird (so liest der Logger die echten Parameter)
  const RadioSettings& cfg = radioSettings();
  mb.addHreg(LORA_FREQ_REGISTER, (uint16_t)(cfg.freqMHz * 10.0f + 0.5f));
  mb.addHreg(LORA_POWER_REGISTER, (uint16_t)(int16_t)cfg.powerDbm);
  mb.addHreg(LORA_SF_REGISTER, (uint16_t)cfg.sf);
  mb.addHreg(LORA_BW_REGISTER, (uint16_t)(cfg.bwKhz + 0.5f));
  mb.addHreg(LORA_CR_REGISTER, (uint16_t)cfg.cr);
  mb.addHreg(LORA_APPLY_CMD, 0);

  // Betriebsart
  mb.addHreg(RADIO_MODE_REQ_REGISTER, RADIO_MODE_LORA);
  mb.addHreg(RADIO_MODE_ACTIVE_REGISTER, RADIO_MODE_LORA);

  // OTA-Register (500..675)
  otaSetupRegisters();
}

void updateLoRaInfo(const String& idStr, uint32_t timestamp, float rssi, float snr) {
  uint16_t receivedId = (uint16_t)idStr.toInt();
  mb.Hreg(RX_ID_REGISTER, receivedId);

  mb.Hreg(TIME_REG_BASE,     (uint16_t)(timestamp >> 16));
  mb.Hreg(TIME_REG_BASE + 1, (uint16_t)(timestamp & 0xFFFF));

  mb.Hreg(RSSI_REGISTER, (int16_t)(rssi * 10.0));
  mb.Hreg(SNR_REGISTER,  isnan(snr) ? 0 : (int16_t)(snr * 10.0));   // FSK: kein SNR → 0

  Serial.println("--- Modbus Update ---");
  Serial.printf("Empfangene ID: %u\n", receivedId);
  Serial.printf("Zeit         : %lu ms\n", timestamp);
  Serial.printf("RSSI         : %.1f dBm\n", rssi);
  Serial.printf("SNR          : %.1f dB\n", snr);
  Serial.println("---------------------");
}

static inline uint16_t _dbmToReg(float dbm) {
  return isnan(dbm) ? 0 : (uint16_t)(int16_t)(dbm * 10.0f);
}

void updateRssiResult(float avg, float minV, float maxV, uint16_t samples) {
  mb.Hreg(CW_RSSI_AVG_REGISTER, _dbmToReg(avg));
  mb.Hreg(CW_RSSI_MAX_REGISTER, _dbmToReg(maxV));
  mb.Hreg(CW_RSSI_MIN_REGISTER, _dbmToReg(minV));
  mb.Hreg(CW_RSSI_COUNT_REGISTER, samples);
  // zuletzt: Master erkennt am neuen SEQ-Wert, dass das Ergebnis vollständig ist
  mb.Hreg(CW_RSSI_SEQ_REGISTER, mb.Hreg(CW_RSSI_SEQ_REGISTER) + 1);
}

// Register 400..404 übernehmen. false, wenn CW läuft (Flag bleibt dann stehen, bis es klappt).
bool applyRadioSettings() {
  RadioSettings s;
  s.freqMHz  = mb.Hreg(LORA_FREQ_REGISTER) / 10.0;
  s.powerDbm = (int16_t)mb.Hreg(LORA_POWER_REGISTER);
  s.bwKhz    = mb.Hreg(LORA_BW_REGISTER);  // normal in kHz

  // optional: Werte einklemmen, damit kein Unsinn gesetzt wird
  s.sf = constrain((int)mb.Hreg(LORA_SF_REGISTER), 7, 12);
  s.cr = constrain((int)mb.Hreg(LORA_CR_REGISTER), 5, 8);

  return radioApplySettings(s);
}

void triggerMeasurement() {
  Serial.println("📏 >> Messung wird ausgelöst...");

  // Marker/TS aus 300/301 (kommt vom Master)
  uint32_t ts = ((uint32_t)mb.Hreg(TX_TIMESTAMP_BASE) << 16) | mb.Hreg(TX_TIMESTAMP_BASE + 1);

  // Payload aus 320.. lesen
  uint16_t len = mb.Hreg(PAYLOAD_LEN_REG);
  if (len > PAYLOAD_MAX_BYTES) len = PAYLOAD_MAX_BYTES;

  String payload = "";
  payload.reserve(len);

  uint16_t words = (len + 1) / 2;
  for (uint16_t i = 0; i < words; i++) {
    uint16_t w = mb.Hreg(PAYLOAD_DATA_BASE + i);
    char c1 = (char)((w >> 8) & 0xFF);
    char c2 = (char)(w & 0xFF);

    if ((uint16_t)payload.length() < len) payload += c1;
    if ((uint16_t)payload.length() < len) payload += c2;
  }

  // IMMER: id@ts@payload  (payload darf "" sein => Nachricht endet mit '@')
  String msg = String(SLAVE_ID) + "@" + String(ts) + "@" + payload;

  Serial.print("📡 TX: ");
  Serial.println(msg);

  int result = radio.transmit(msg);
  if (result == RADIOLIB_ERR_NONE) {
    Serial.println("✅ Messdaten gesendet.");
  } else {
    Serial.printf("❌ Sende-Fehler: %d\n", result);
  }

  radio.startReceive();  // zurück in Empfang
}

// Befehle, die nur in einer Betriebsart sinnvoll sind: sonst loggen und ignorieren.
// (Der Master prüft vor jeder Runde Register 421; das hier ist nur das Sicherheitsnetz.)
static inline bool _needMode(RadioMode mode, const char* what) {
  if (radioReady() && radioMode() == mode) return true;
  Serial.printf("⚠️ %s ignoriert: Radio nicht im %s-Modus\n", what,
                mode == RADIO_MODE_LORA ? "LoRa" : mode == RADIO_MODE_FSK ? "FSK" : "CW");
  return false;
}

// Paket senden geht in LoRa und FSK
static inline bool _needPacketMode(const char* what) {
  if (radioReady() && _isPacketMode(radioMode())) return true;
  Serial.printf("⚠️ %s ignoriert: Radio nicht im LoRa-/FSK-Modus\n", what);
  return false;
}

void modbusTask() {
  mb.task();
  yield();
  otaSyncRegisters();  // OTA-Status/Fortschritt fuer den Master (500..502)

  // Betriebsart: Wunsch des Masters übernehmen (umgeschaltet wird in radioTask()), Ist-Zustand melden
  uint16_t modeReq = mb.Hreg(RADIO_MODE_REQ_REGISTER);
  if (modeReq == RADIO_MODE_LORA || modeReq == RADIO_MODE_CW || modeReq == RADIO_MODE_FSK) radioRequestMode((RadioMode)modeReq);
  mb.Hreg(RADIO_MODE_ACTIVE_REGISTER, radioActiveReg());
  mb.Hreg(CW_STATE_REGISTER, cwState());

  uint16_t cmd = mb.Hreg(CMD_REGISTER);
  if (cmd != lastCmdVal) {
    Serial.printf("🔍 Neuer CMD-Wert: %d\n", cmd);
    lastCmdVal = cmd;

    switch (cmd) {
      case 0:
        break;  // Reset des CMD-Registers (siehe unten)

      case 1:
        Serial.println("🔧 Befehl: LED an");
        heltec_led(100);   // 0..255
        break;

      case 2:
        Serial.println("🔧 Befehl: LED aus");
        heltec_led(0);
        break;

      case 10:
        Serial.println("📡 Befehl: Paket-Broadcast (LoRa/FSK)");
        if (_needPacketMode("Paket-Broadcast")) triggerMeasurement();
        break;

      case 20: {
        uint16_t newId = mb.Hreg(NEW_ID_REGISTER);
        mb.Hreg(NEW_ID_REGISTER, 0);
        if (newId < 1 || newId > 247) {
          Serial.printf("⚠️ Ungültige neue Slave-ID: %u\n", newId);
          break;
        }
        Serial.printf("🆔 Befehl: Slave-ID %u -> %u, Neustart\n", SLAVE_ID, newId);
        saveSlaveId((uint8_t)newId);
        delay(100);  // Modbus-Antwort fertig senden
        ESP.restart();
        break;
      }

      case 21:
        Serial.printf("🆔 Befehl: Slave-ID %u loeschen, Neustart mit neuer Zufalls-ID\n", SLAVE_ID);
        saveSlaveId(0);
        delay(100);  // Modbus-Antwort fertig senden
        ESP.restart();
        break;

      case 22: {
        char target[13];
        for (int i = 0; i < 6; i++) {
          uint16_t w = mb.Hreg(TARGET_MAC_BASE + i);
          target[2 * i]     = (char)(w >> 8);
          target[2 * i + 1] = (char)(w & 0xFF);
          mb.Hreg(TARGET_MAC_BASE + i, 0);
        }
        target[12] = '\0';
        uint16_t newId = mb.Hreg(NEW_ID_REGISTER);
        mb.Hreg(NEW_ID_REGISTER, 0);
        if (!deviceId.equalsIgnoreCase(target)) break;  // anderes Board gemeint
        if (newId < 1 || newId > 247) {
          Serial.printf("⚠️ Ungültige neue Slave-ID: %u\n", newId);
          break;
        }
        Serial.printf("🆔 Befehl: Slave-ID per MAC %u -> %u, Neustart\n", SLAVE_ID, newId);
        saveSlaveId((uint8_t)newId);
        delay(100);
        ESP.restart();
        break;
      }

      case CMD_CW_START:
        Serial.println("📡 Befehl: CW senden");
        if (_needMode(RADIO_MODE_CW, "CW senden") && !cwStartTx(mb.Hreg(CW_TX_MS_REGISTER))) {
          Serial.println("⚠️ CW nicht gestartet (beschäftigt oder Fehler)");
        }
        break;

      case CMD_CW_STOP:
        Serial.println("🛑 Befehl: CW / Messung stoppen");
        if (_needMode(RADIO_MODE_CW, "CW stoppen")) cwStop();
        break;

      case CMD_RSSI_MEASURE:
        Serial.println("📏 Befehl: RSSI messen");
        if (_needMode(RADIO_MODE_CW, "RSSI messen") && !rssiStartMeasure(mb.Hreg(RSSI_MEASURE_MS_REGISTER))) {
          Serial.println("⚠️ Messung nicht gestartet (beschäftigt)");
        }
        break;

      case CMD_OTA_START:
        Serial.println("🔄 Befehl: OTA-Update");
        otaStart();
        break;

      case CMD_OTA_ABORT:
        Serial.println("⏹️ Befehl: OTA abbrechen");
        otaAbort();
        break;

      case 99:
        Serial.println("🔁 Befehl: Neustart");
        ESP.restart();
        break;

      default:
        Serial.printf("⚠️ Unbekannter Befehl: %d\n", cmd);
        break;
    }

    // Befehl zurücksetzen, damit der nächste CMD-Wechsel erkannt wird
    mb.Hreg(CMD_REGISTER, 0);
  }

  // Radio-Settings unabhängig von CMD-Änderung übernehmen – bei laufendem CW erst danach
  if (mb.Hreg(LORA_APPLY_CMD) == 1) {
    if (applyRadioSettings()) {
      mb.Hreg(LORA_APPLY_CMD, 0);  // zurücksetzen
    }
  }
}
