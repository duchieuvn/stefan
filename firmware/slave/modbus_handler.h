#pragma once
#include <ModbusRTU.h>
#include "lora_handler.h"
#include "heltec_led.h"
#include "ota_handler.h"
#include "device_id.h"

extern SX1262 radio;
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
// 10  = LoRa-Broadcast
// 11  = BME280-Messung ausführen
// 20  = Slave-ID aus Register 11 im NVS speichern + Neustart
// 21  = gespeicherte Slave-ID loeschen + Neustart (→ neue Zufalls-ID)
// 22  = nur wenn MAC = Reg 12..17: ID aus Reg 11 speichern + Neustart (per Broadcast an alle)
// 50  = OTA-Update starten (Parameter ab Register 510, siehe ota_handler.h)
// 51  = OTA-Update abbrechen
// 99  = Neustart

const uint16_t TX_TIMESTAMP_BASE   = 300;  // 2x16bit → Timestamp zum Senden

// LoRa-Parameter Register
const uint16_t LORA_FREQ_REGISTER     = 400;  // in MHz × 10, z. B. 8680
const uint16_t LORA_POWER_REGISTER    = 401;  // in dBm
const uint16_t LORA_SF_REGISTER       = 402;  // Spreading Factor (7–12)
const uint16_t LORA_BW_REGISTER       = 403;  // Bandwidth in kHz (z. B. 125)
const uint16_t LORA_CR_REGISTER       = 404;  // Coding Rate (5 = 4/5 ... 8 = 4/8)
const uint16_t LORA_APPLY_CMD         = 410;  // 1 = übernehmen

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

  // Steuerung & Sendezeit
  mb.addHreg(CMD_REGISTER, 0);
  mb.addHreg(TX_TIMESTAMP_BASE, 0);
  mb.addHreg(TX_TIMESTAMP_BASE + 1, 0);

  // Payload Register (für id@ts@payload)
  mb.addHreg(PAYLOAD_LEN_REG, 0);
  for (int i = 0; i < PAYLOAD_WORDS; i++) {
    mb.addHreg(PAYLOAD_DATA_BASE + i, 0);
  }

  // LoRa-Konfiguration als Holding-Register ablegen (z. B. Standardwerte)
  mb.addHreg(LORA_FREQ_REGISTER, 4330);
  mb.addHreg(LORA_POWER_REGISTER, 22);
  mb.addHreg(LORA_SF_REGISTER, 12);
  mb.addHreg(LORA_BW_REGISTER, 125);
  mb.addHreg(LORA_CR_REGISTER, 5);
  mb.addHreg(LORA_APPLY_CMD, 0);

  // OTA-Register (500..675)
  otaSetupRegisters();
}

void updateLoRaInfo(const String& idStr, uint32_t timestamp, float rssi, float snr) {
  uint16_t receivedId = (uint16_t)idStr.toInt();
  mb.Hreg(RX_ID_REGISTER, receivedId);

  mb.Hreg(TIME_REG_BASE,     (uint16_t)(timestamp >> 16));
  mb.Hreg(TIME_REG_BASE + 1, (uint16_t)(timestamp & 0xFFFF));

  mb.Hreg(RSSI_REGISTER, (int16_t)(rssi * 10.0));
  mb.Hreg(SNR_REGISTER,  (int16_t)(snr  * 10.0));

  Serial.println("--- Modbus Update ---");
  Serial.printf("Empfangene ID: %u\n", receivedId);
  Serial.printf("Zeit         : %lu ms\n", timestamp);
  Serial.printf("RSSI         : %.1f dBm\n", rssi);
  Serial.printf("SNR          : %.1f dB\n", snr);
  Serial.println("---------------------");
}

void applyLoRaSettings() {
  float freqMHz = mb.Hreg(LORA_FREQ_REGISTER) / 10.0;
  int power = mb.Hreg(LORA_POWER_REGISTER);
  int sf = mb.Hreg(LORA_SF_REGISTER);
  float bw = mb.Hreg(LORA_BW_REGISTER);  // normal in kHz
  int cr = mb.Hreg(LORA_CR_REGISTER);

  // optional: Werte einklemmen, damit kein Unsinn gesetzt wird
  sf = constrain(sf, 7, 12);
  cr = constrain(cr, 5, 8);

  Serial.println("⚙️  Übernehme LoRa-Konfiguration:");
  Serial.printf("Frequenz      : %.1f MHz\n", freqMHz);
  Serial.printf("Sendeleistung : %d dBm\n", power);
  Serial.printf("Spreading     : SF%d\n", sf);
  Serial.printf("Bandwidth     : %.0f kHz\n", bw);
  Serial.printf("Coding Rate   : 4/%d\n", cr);

  radio.setFrequency(freqMHz);
  radio.setOutputPower(power);
  radio.setSpreadingFactor(sf);
  radio.setBandwidth(bw);
  radio.setCodingRate(cr);
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
    Serial.println("✅ Messdaten über LoRa gesendet.");
  } else {
    Serial.printf("❌ Sende-Fehler: %d\n", result);
  }

  radio.startReceive();  // zurück in Empfang
}

void modbusTask() {
  mb.task();
  yield();
  otaSyncRegisters();  // OTA-Status/Fortschritt fuer den Master (500..502)

  uint16_t cmd = mb.Hreg(CMD_REGISTER);
  if (cmd != lastCmdVal) {
    Serial.printf("🔍 Neuer CMD-Wert: %d\n", cmd);
    lastCmdVal = cmd;

    switch (cmd) {
      case 1:
        Serial.println("🔧 Befehl: LED an");
        heltec_led(100);   // 0..255
        break;

      case 2:
        Serial.println("🔧 Befehl: LED aus");
        heltec_led(0);   
        break;
      
      case 10:
        Serial.println("📡 Befehl: LoRa-Broadcast");
        triggerMeasurement();
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

  // LoRa-Settings unabhängig von CMD-Änderung übernehmen
  uint16_t applyFlag = mb.Hreg(LORA_APPLY_CMD);
  if (applyFlag == 1) {
    applyLoRaSettings();
    mb.Hreg(LORA_APPLY_CMD, 0);  // zurücksetzen
  }
}
