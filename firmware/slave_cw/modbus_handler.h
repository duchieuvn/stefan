#pragma once
#include <ModbusRTU.h>
#include "cw_handler.h"
#include "heltec_led.h"

extern String deviceId;  // wird im Setup gesetzt (getMacFull())

// ------------------------------
// 🆔 Eigene Gerätekennung
// ------------------------------
extern const uint8_t SLAVE_ID;

// Change these pins according to your TTL ebay modbus
#define TXD1 6
#define RXD1 7

// ------------------------------
// 📦 Modbus-Register-Belegung
// ------------------------------
// Eigene Kennung
const uint16_t OWN_ID_REGISTER     = 10;   // eigene Slave-ID
const uint16_t OWN_MAC_REG_BASE    = 20;   // eigene MAC (12x ASCII-Zeichen)

// Ergebnis der letzten RSSI-Messung (CMD 22)
const uint16_t RSSI_AVG_REGISTER   = 114;  // int16_t ×10, Mittelwert (linear gemittelt)
const uint16_t RSSI_RESERVED_REG   = 115;  // immer 0 – nur damit 114..119 am Stück lesbar ist
const uint16_t RSSI_MAX_REGISTER   = 116;  // int16_t ×10
const uint16_t RSSI_MIN_REGISTER   = 117;  // int16_t ×10
const uint16_t RSSI_COUNT_REGISTER = 118;  // Anzahl Samples
const uint16_t RSSI_SEQ_REGISTER   = 119;  // +1 nach jeder fertigen Messung

// Steuern / Triggern
const uint16_t CMD_REGISTER        = 200;  // Steuerung durch Master

// CMD-Konvention:
//  1  = LED an
//  2  = LED aus
// 20  = CW-Träger senden (Dauer aus CW_TX_MS_REGISTER)
// 21  = CW-Sendung / RSSI-Messung abbrechen
// 22  = RSSI messen (Dauer aus RSSI_MEASURE_MS_REGISTER)
// 99  = Neustart

const uint16_t CW_TX_MS_REGISTER        = 330;  // CW-Dauer in ms (max CW_MAX_TX_MS)
const uint16_t RSSI_MEASURE_MS_REGISTER = 331;  // Messdauer in ms (max CW_MAX_MEASURE_MS)
const uint16_t CW_STATE_REGISTER        = 340;  // nur lesen: 0 = IDLE, 1 = CW TX, 2 = Messung

// Radio-Parameter Register (gleiche Adressen wie LoRa-Firmware, damit das Backend weiter lesen/schreiben kann)
const uint16_t LORA_FREQ_REGISTER     = 400;  // in MHz × 10, z. B. 4330
const uint16_t LORA_POWER_REGISTER    = 401;  // in dBm
const uint16_t LORA_SF_REGISTER       = 402;  // wird bei CW ignoriert
const uint16_t LORA_BW_REGISTER       = 403;  // wird bei CW ignoriert
const uint16_t LORA_CR_REGISTER       = 404;  // wird bei CW ignoriert
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
  for (int i = 0; i < 12; i++) {
    mb.addHreg(OWN_MAC_REG_BASE + i, deviceId[i]);
  }

  // RSSI-Ergebnis-Register (mit Initialwert 0)
  mb.addHreg(RSSI_AVG_REGISTER, 0);
  mb.addHreg(RSSI_RESERVED_REG, 0);
  mb.addHreg(RSSI_MAX_REGISTER, 0);
  mb.addHreg(RSSI_MIN_REGISTER, 0);
  mb.addHreg(RSSI_COUNT_REGISTER, 0);
  mb.addHreg(RSSI_SEQ_REGISTER, 0);

  // Steuerung
  mb.addHreg(CMD_REGISTER, 0);
  mb.addHreg(CW_TX_MS_REGISTER, 2000);
  mb.addHreg(RSSI_MEASURE_MS_REGISTER, 1000);
  mb.addHreg(CW_STATE_REGISTER, 0);

  // Radio-Konfiguration als Holding-Register ablegen (Standardwerte)
  mb.addHreg(LORA_FREQ_REGISTER, (uint16_t)(CW_FREQ_MHZ * 10.0f + 0.5f));
  mb.addHreg(LORA_POWER_REGISTER, CW_TX_POWER_DBM);
  mb.addHreg(LORA_SF_REGISTER, 0);
  mb.addHreg(LORA_BW_REGISTER, 0);
  mb.addHreg(LORA_CR_REGISTER, 0);
  mb.addHreg(LORA_APPLY_CMD, 0);
}

static inline uint16_t _dbmToReg(float dbm) {
  return isnan(dbm) ? 0 : (uint16_t)(int16_t)(dbm * 10.0f);
}

void updateRssiResult(float avg, float minV, float maxV, uint16_t samples) {
  mb.Hreg(RSSI_AVG_REGISTER, _dbmToReg(avg));
  mb.Hreg(RSSI_MAX_REGISTER, _dbmToReg(maxV));
  mb.Hreg(RSSI_MIN_REGISTER, _dbmToReg(minV));
  mb.Hreg(RSSI_COUNT_REGISTER, samples);
  // zuletzt: Master erkennt am neuen SEQ-Wert, dass das Ergebnis vollständig ist
  mb.Hreg(RSSI_SEQ_REGISTER, mb.Hreg(RSSI_SEQ_REGISTER) + 1);
}

void modbusTask() {
  mb.task();
  yield();

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

      case 20:
        Serial.println("📡 Befehl: CW senden");
        if (!cwStartTx(mb.Hreg(CW_TX_MS_REGISTER))) {
          Serial.println("⚠️ CW nicht gestartet (beschäftigt oder Fehler)");
        }
        break;

      case 21:
        Serial.println("🛑 Befehl: CW / Messung stoppen");
        cwStop();
        break;

      case 22:
        Serial.println("📏 Befehl: RSSI messen");
        if (!rssiStartMeasure(mb.Hreg(RSSI_MEASURE_MS_REGISTER))) {
          Serial.println("⚠️ Messung nicht gestartet (beschäftigt)");
        }
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

  // Radio-Settings übernehmen – nur im IDLE, sonst bleibt das Flag stehen bis CW/Messung fertig ist
  if (mb.Hreg(LORA_APPLY_CMD) == 1) {
    float freqMHz = mb.Hreg(LORA_FREQ_REGISTER) / 10.0;
    int power = (int16_t)mb.Hreg(LORA_POWER_REGISTER);
    if (cwApplyRadio(freqMHz, power)) {
      mb.Hreg(LORA_APPLY_CMD, 0);  // zurücksetzen
    }
  }

  mb.Hreg(CW_STATE_REGISTER, cwState());
}
