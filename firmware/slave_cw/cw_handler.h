#ifndef CW_HANDLER_H
#define CW_HANDLER_H

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "oled_ui.h"

// ============================================================
// Continuous Wave (CW) + RSSI-Messung
//  - Sender:    unmodulierter Träger für N ms (transmitDirect)
//  - Empfänger: misst N ms lang die momentane RSSI (getRSSI(false))
// Alles non-blocking, damit Modbus während CW/Messung erreichbar bleibt.
// ============================================================

// Heltec WiFi LoRa 32 V3.x (SX1262) – Default Pinout
#ifndef LORA_PIN_SCK
  #define LORA_PIN_SCK   9
#endif
#ifndef LORA_PIN_MISO
  #define LORA_PIN_MISO  11
#endif
#ifndef LORA_PIN_MOSI
  #define LORA_PIN_MOSI  10
#endif
#ifndef LORA_PIN_NSS
  #define LORA_PIN_NSS   8
#endif
#ifndef LORA_PIN_RST
  #define LORA_PIN_RST   12
#endif
#ifndef LORA_PIN_BUSY
  #define LORA_PIN_BUSY  13
#endif
#ifndef LORA_PIN_DIO1
  #define LORA_PIN_DIO1  14
#endif

// Debug-Ausgaben
#ifndef CW_DEBUG
  #define CW_DEBUG 1
#endif

// Standard-Parameter (kannst du überschreiben)
#ifndef CW_FREQ_MHZ
  #define CW_FREQ_MHZ 433.0f
#endif
#ifndef CW_TX_POWER_DBM
  #define CW_TX_POWER_DBM 22
#endif
// RX-Bandbreite für die RSSI-Messung: schmal = niedriger Rauschboden,
// muss aber den Frequenzversatz zwischen Sender und Empfänger abdecken.
// Muss ein SX126x-Wert sein (4.8, 5.8, 7.3, 9.7, 11.7, 14.6, 19.5, 23.4, 29.3, 39.0, ...)
#ifndef CW_RX_BW_KHZ
  #define CW_RX_BW_KHZ 11.7f
#endif
// Sicherheitslimits (433 MHz: 10 % Duty Cycle in der EU, PA-Erwärmung bei 22 dBm)
#ifndef CW_MAX_TX_MS
  #define CW_MAX_TX_MS 10000
#endif
#ifndef CW_MAX_MEASURE_MS
  #define CW_MAX_MEASURE_MS 10000
#endif
// Abstand zwischen zwei RSSI-Samples
#ifndef CW_RSSI_SAMPLE_MS
  #define CW_RSSI_SAMPLE_MS 5
#endif

// ============================================================
// Externe Dinge (werden woanders definiert)
// ============================================================

// WICHTIG: radio darf genau 1x in einer .ino/.cpp Datei instanziiert werden!
extern SX1262 radio;
extern const uint8_t SLAVE_ID;

// Kommt aus modbus_handler.h: Messergebnis in die Register schreiben
void updateRssiResult(float avg, float minV, float maxV, uint16_t samples);

// ============================================================
// Interner Zustand
// ============================================================
enum CwState : uint8_t {
  CW_IDLE    = 0,   // Empfang, wartet auf Befehl
  CW_TX      = 1,   // sendet Träger
  CW_MEASURE = 2    // misst RSSI
};

static CwState  _cwState      = CW_IDLE;
static uint32_t _cwStartMs    = 0;
static uint32_t _cwDurationMs = 0;

static uint32_t _rssiNextSampleMs = 0;
static double   _rssiSumMw = 0.0;   // Mittelung linear in mW, nicht in dBm
static float    _rssiMin   = 0.0f;
static float    _rssiMax   = 0.0f;
static uint16_t _rssiCount = 0;

static float    _lastRssiAvg = NAN;
static float    _lastRssiMax = NAN;
static uint32_t _lastUiMs    = 0;

// ============================================================
// Hilfsfunktionen
// ============================================================
static inline const char* _cwStateName(CwState s) {
  switch (s) {
    case CW_TX:      return "CW TX";
    case CW_MEASURE: return "MESSUNG";
    default:         return "IDLE";
  }
}

static inline void _cwDrawUi() {
  oledDrawCw(_lastRssiAvg, _lastRssiMax, _cwStateName(_cwState), SLAVE_ID);
}

// Zurück in Dauer-Empfang (nötig für getRSSI(false))
static inline void _cwBackToRx() {
  radio.standby();
  radio.startReceive();
}

static inline void _rssiFinish() {
  float avg = NAN;
  if (_rssiCount > 0) {
    avg = 10.0f * log10f((float)(_rssiSumMw / _rssiCount));
  }

  _lastRssiAvg = avg;
  _lastRssiMax = _rssiMax;
  _cwState = CW_IDLE;

  updateRssiResult(avg, _rssiMin, _rssiMax, _rssiCount);

#if CW_DEBUG
  Serial.println("---- RSSI-Messung fertig ----");
  Serial.printf("Samples     : %u\n", _rssiCount);
  Serial.printf("RSSI avg    : %.1f dBm\n", avg);
  Serial.printf("RSSI min    : %.1f dBm\n", _rssiMin);
  Serial.printf("RSSI max    : %.1f dBm\n", _rssiMax);
  Serial.println("-----------------------------");
#endif

  _cwDrawUi();
}

// ============================================================
// Public API (Header-only)
// ============================================================

inline void initRadio() {
  delay(50);
  SPI.begin(LORA_PIN_SCK, LORA_PIN_MISO, LORA_PIN_MOSI, LORA_PIN_NSS);

  // FSK-Modem nur als Träger für Frequenz/Leistung/RX-Bandbreite – Pakete werden nicht benutzt
  int st = radio.beginFSK((float)CW_FREQ_MHZ, 4.8f, 5.0f, (float)CW_RX_BW_KHZ, (int)CW_TX_POWER_DBM);
#if CW_DEBUG
  Serial.printf("[CW] beginFSK -> %d\n", st);
#endif
  if (st != RADIOLIB_ERR_NONE) {
#if CW_DEBUG
    Serial.println("❌ SX1262 Fehler bei beginFSK()");
#endif
    while (true) { delay(100); }
  }

  // Heltec V3 hat RF-Switch über DIO2 → wichtig
  radio.setDio2AsRfSwitch(true);

#if CW_DEBUG
  Serial.println("⚙️  CW Settings:");
  Serial.printf("  Freq   : %.3f MHz\n", (float)CW_FREQ_MHZ);
  Serial.printf("  Power  : %d dBm\n", (int)CW_TX_POWER_DBM);
  Serial.printf("  RX BW  : %.1f kHz\n", (float)CW_RX_BW_KHZ);
#endif

  radio.startReceive();
}

inline uint8_t cwState() {
  return (uint8_t)_cwState;
}

// Träger für durationMs senden. false, wenn gerade beschäftigt oder Fehler.
inline bool cwStartTx(uint32_t durationMs) {
  if (_cwState != CW_IDLE) return false;
  durationMs = constrain(durationMs, (uint32_t)1, (uint32_t)CW_MAX_TX_MS);

  radio.standby();
  int st = radio.transmitDirect();
  if (st != RADIOLIB_ERR_NONE) {
#if CW_DEBUG
    Serial.printf("❌ transmitDirect -> %d\n", st);
#endif
    radio.startReceive();
    return false;
  }

  _cwState = CW_TX;
  _cwStartMs = millis();
  _cwDurationMs = durationMs;
#if CW_DEBUG
  Serial.printf("📡 CW TX gestartet (%lu ms)\n", (unsigned long)durationMs);
#endif
  _cwDrawUi();
  return true;
}

// RSSI für durationMs messen. false, wenn gerade beschäftigt.
inline bool rssiStartMeasure(uint32_t durationMs) {
  if (_cwState != CW_IDLE) return false;
  durationMs = constrain(durationMs, (uint32_t)1, (uint32_t)CW_MAX_MEASURE_MS);

  _rssiSumMw = 0.0;
  _rssiMin = NAN;   // werden beim ersten Sample gesetzt
  _rssiMax = NAN;
  _rssiCount = 0;

  _cwState = CW_MEASURE;
  _cwStartMs = millis();
  _cwDurationMs = durationMs;
  _rssiNextSampleMs = _cwStartMs;
#if CW_DEBUG
  Serial.printf("📏 RSSI-Messung gestartet (%lu ms)\n", (unsigned long)durationMs);
#endif
  _cwDrawUi();
  return true;
}

// CW-Sendung bzw. Messung sofort abbrechen (Messung liefert dann kein Ergebnis)
inline void cwStop() {
  if (_cwState == CW_TX) {
    _cwBackToRx();
#if CW_DEBUG
    Serial.println("🛑 CW TX gestoppt");
#endif
  } else if (_cwState == CW_MEASURE) {
#if CW_DEBUG
    Serial.println("🛑 RSSI-Messung abgebrochen");
#endif
  }
  _cwState = CW_IDLE;
  _cwDrawUi();
}

// Frequenz/Leistung übernehmen – nur im IDLE, sonst false (später erneut versuchen)
inline bool cwApplyRadio(float freqMHz, int powerDbm) {
  if (_cwState != CW_IDLE) return false;

  radio.standby();
  int stF = radio.setFrequency(freqMHz);
  int stP = radio.setOutputPower(powerDbm);
  radio.startReceive();

#if CW_DEBUG
  Serial.println("⚙️  Übernehme CW-Konfiguration:");
  Serial.printf("Frequenz      : %.1f MHz (-> %d)\n", freqMHz, stF);
  Serial.printf("Sendeleistung : %d dBm (-> %d)\n", powerDbm, stP);
#endif
  return true;
}

// Im loop() aufrufen
inline void cwTask() {
  uint32_t now = millis();

  if (_cwState == CW_TX) {
    if (now - _cwStartMs >= _cwDurationMs) {
      _cwBackToRx();
      _cwState = CW_IDLE;
#if CW_DEBUG
      Serial.println("✅ CW TX beendet");
#endif
      _cwDrawUi();
    }
    return;
  }

  if (_cwState == CW_MEASURE) {
    if ((int32_t)(now - _rssiNextSampleMs) >= 0) {
      float r = radio.getRSSI(false);   // momentane RSSI, nicht Paket-RSSI
      _rssiSumMw += pow(10.0, r / 10.0);
      if (_rssiCount == 0 || r < _rssiMin) _rssiMin = r;
      if (_rssiCount == 0 || r > _rssiMax) _rssiMax = r;
      _rssiCount++;
      _rssiNextSampleMs = now + CW_RSSI_SAMPLE_MS;
    }
    if (now - _cwStartMs >= _cwDurationMs) {
      _rssiFinish();
    }
  }
}

inline void cwUiTask(uint32_t periodMs = 1000) {
  uint32_t now = millis();
  if (now - _lastUiMs < periodMs) return;
  _lastUiMs = now;
  _cwDrawUi();
}

#endif // CW_HANDLER_H
