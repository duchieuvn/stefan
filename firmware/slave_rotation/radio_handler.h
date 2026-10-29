#ifndef RADIO_HANDLER_H
#define RADIO_HANDLER_H

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "oled_ui.h"

// ============================================================
// Ein SX1262, drei Betriebsarten – der Master schaltet im Wechsel um:
//  - RADIO_MODE_LORA: Pakete "id@ts@payload" senden/empfangen (IRQ, non-blocking RX)
//  - RADIO_MODE_CW:   unmodulierter Träger senden / RSSI messen (FSK-Modem als Träger)
//  - RADIO_MODE_FSK:  wie LoRa, aber GFSK-Pakete (Sync-Wort, CRC); kein SNR
// LoRa hört kein FSK und umgekehrt → alle Nodes müssen im selben Modus sein.
// Der Modus wird über Register 420 gewünscht und in Register 421 gemeldet
// (siehe modbus_handler.h); umgeschaltet wird nur im Leerlauf, nie mitten in
// einer CW-Sendung/Messung.
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
#ifndef RADIO_DEBUG
  #define RADIO_DEBUG 1
#endif

// Pause zwischen zwei Versuchen, wenn das Radio sich nicht initialisieren lässt
#ifndef RADIO_RETRY_MS
  #define RADIO_RETRY_MS 2000
#endif

// Frequenz/Leistung gelten für beide Modi
#ifndef RADIO_FREQ_MHZ
  #define RADIO_FREQ_MHZ 433.0f
#endif
#ifndef RADIO_TX_POWER_DBM
  #define RADIO_TX_POWER_DBM 22
#endif

// LoRa-Parameter (nur im LoRa-Modus)
#ifndef LORA_SF
  #define LORA_SF 7
#endif
#ifndef LORA_BW_KHZ
  #define LORA_BW_KHZ 125.0f
#endif
#ifndef LORA_CR
  #define LORA_CR 5   // 5..8 entspricht 4/5..4/8
#endif

// CW-Parameter (nur im CW-Modus)
// FSK-Modem nur als Träger für Frequenz/Leistung/RX-Bandbreite – Pakete werden nicht benutzt
#ifndef CW_BITRATE_KBPS
  #define CW_BITRATE_KBPS 4.8f
#endif
#ifndef CW_FREQ_DEV_KHZ
  #define CW_FREQ_DEV_KHZ 5.0f
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

// FSK-Paket-Parameter (nur im FSK-Modus, wie slave_fsk/fsk_handler.h)
#ifndef FSK_BITRATE_KBPS
  #define FSK_BITRATE_KBPS 4.8f
#endif
#ifndef FSK_FREQ_DEV_KHZ
  #define FSK_FREQ_DEV_KHZ 5.0f
#endif
// Muss ein SX126x-Wert sein (4.8, 5.8, 7.3, ... 39.0, 46.9, ... 467.0)
#ifndef FSK_RX_BW_KHZ
  #define FSK_RX_BW_KHZ 39.0f
#endif
#ifndef FSK_PREAMBLE_LEN
  #define FSK_PREAMBLE_LEN 16   // in Bits
#endif
static uint8_t _fskSyncWord[] = {0x2D, 0xD4};

// ============================================================
// Externe Dinge (werden woanders definiert)
// ============================================================

// WICHTIG: radio darf genau 1x in einer .ino/.cpp Datei instanziiert werden!
extern SX1262 radio;
extern uint8_t SLAVE_ID;  // aus NVS, siehe device_id.h

// Kommen aus modbus_handler.h: Ergebnisse in die Register schreiben
void updateLoRaInfo(const String& idStr, uint32_t timestamp, float rssi, float snr);
void updateRssiResult(float avg, float minV, float maxV, uint16_t samples);

// ============================================================
// Interner Zustand (static, damit Header-Only ohne Mehrfachdef.)
// ============================================================
enum RadioMode : uint8_t {
  RADIO_MODE_LORA = 0,
  RADIO_MODE_CW   = 1,
  RADIO_MODE_FSK  = 2
};

// LoRa und FSK empfangen/senden Pakete (DIO1-IRQ); CW nicht
static inline bool _isPacketMode(RadioMode m) { return m == RADIO_MODE_LORA || m == RADIO_MODE_FSK; }
// Statt eines Modus im "aktiv"-Register, solange das Radio nicht initialisiert ist
static const uint16_t RADIO_MODE_FAILED = 0xFFFF;

// Aktuelle Radio-Einstellungen; die Register 400..404 werden daraus vorbelegt
struct RadioSettings {
  float freqMHz;
  int   powerDbm;
  int   sf;
  float bwKhz;
  int   cr;
};
static RadioSettings _radioCfg = { RADIO_FREQ_MHZ, RADIO_TX_POWER_DBM, LORA_SF, LORA_BW_KHZ, LORA_CR };

static RadioMode _radioMode = RADIO_MODE_LORA;   // Modus, auf den der Chip gerade konfiguriert ist
static RadioMode _radioWant = RADIO_MODE_LORA;   // Modus, den der Master wünscht
static bool      _radioReady = false;            // false = Chip nicht (richtig) initialisiert
static uint32_t  _radioRetryAtMs = 0;

// OLED-Refresh, damit Anzeige „lebt“
static uint32_t _lastUiMs = 0;

// --- LoRa ---
static String _lastMsg = "";
static float  _lastRSSI = NAN;
static float  _lastSNR  = NAN;
static String _lastPayload = "";

static volatile bool _loraRxFlag = false;
static volatile bool _loraIrqEnabled = false;

// RadioLib ruft diese ISR bei DIO1-IRQ auf
static void IRAM_ATTR _onLoraDio1() {
  if (!_loraIrqEnabled) return;
  _loraRxFlag = true;
}

// --- CW ---
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

// ============================================================
// Anzeige
// ============================================================
static inline const char* _cwStateName(CwState s) {
  switch (s) {
    case CW_TX:      return "TX";
    case CW_MEASURE: return "MESSUNG";
    default:         return "IDLE";
  }
}

static inline void _radioDrawUi() {
  if (_radioMode == RADIO_MODE_CW) {
    oledDrawCw(_lastRssiAvg, _lastRssiMax, _cwStateName(_cwState), SLAVE_ID);
  } else {
    oledDrawRssiSnr(_lastRSSI, _lastSNR, SLAVE_ID);
  }
}

inline void radioUiTask(uint32_t periodMs = 1000) {
  uint32_t now = millis();
  if (now - _lastUiMs < periodMs) return;
  _lastUiMs = now;

  // auch ohne neue Pakete/Messungen: Werte erneut zeichnen
  _radioDrawUi();
}

// ============================================================
// LoRa: Empfang
// ============================================================
static inline void _handleNewMessage(const String& msg, float rssi, float snr) {
  // Optional: Duplicate-Filter
  if (msg == _lastMsg) return;
  _lastMsg = msg;

  // Erwartet: id@ts  oder  id@ts@payload  (payload darf leer sein)
  int i1 = msg.indexOf('@');
  if (i1 <= 0) return;

  int i2 = msg.indexOf('@', i1 + 1); // -1 => altes Format id@ts

  String idStr;
  uint32_t ts = 0;
  String payload = "";

  if (i2 < 0) {
    // altes Format: id@timestamp
    idStr = msg.substring(0, i1);
    ts    = (uint32_t)msg.substring(i1 + 1).toInt();
  } else {
    // neues Format: id@timestamp@payload (payload kann "" sein)
    idStr   = msg.substring(0, i1);
    ts      = (uint32_t)msg.substring(i1 + 1, i2).toInt();
    payload = msg.substring(i2 + 1);   // ergibt "" wenn Nachricht mit '@' endet
  }

  _lastRSSI = rssi;
  _lastSNR  = snr;
  _lastPayload = payload;

  // Payload ignorieren ist ok
  updateLoRaInfo(idStr, ts, rssi, snr);

#if RADIO_DEBUG
  Serial.println("---- Neue LoRa-Nachricht ----");
  Serial.printf("Absender ID : %s\n", idStr.c_str());
  Serial.printf("Zeitstempel : %lu ms\n", (unsigned long)ts);
  Serial.printf("RSSI        : %.1f dBm\n", rssi);
  Serial.printf("SNR         : %.1f dB\n", snr);
  Serial.printf("Payload     : '%s'\n", payload.c_str()); // leer => ''
  Serial.println("-----------------------------");
#endif

  oledDrawRssiSnr(_lastRSSI, _lastSNR, SLAVE_ID);
}

// Non-blocking (IRQ): nur im LoRa-Modus aufrufen
inline void runReceiver() {
  if (!_loraRxFlag) return;

  _loraIrqEnabled = false;
  _loraRxFlag = false;

  String msg;
  int state = radio.readData(msg);

  if (state == RADIOLIB_ERR_NONE) {
    float rssi = radio.getRSSI();
    float snr  = (_radioMode == RADIO_MODE_FSK) ? NAN : radio.getSNR();   // FSK hat kein SNR
    _handleNewMessage(msg, rssi, snr);
  }

  radio.startReceive();
  _loraIrqEnabled = true;
}

// ============================================================
// CW: Träger senden / RSSI messen (alles non-blocking)
// ============================================================

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

#if RADIO_DEBUG
  Serial.println("---- RSSI-Messung fertig ----");
  Serial.printf("Samples     : %u\n", _rssiCount);
  Serial.printf("RSSI avg    : %.1f dBm\n", avg);
  Serial.printf("RSSI min    : %.1f dBm\n", _rssiMin);
  Serial.printf("RSSI max    : %.1f dBm\n", _rssiMax);
  Serial.println("-----------------------------");
#endif

  _radioDrawUi();
}

inline uint8_t cwState() {
  return (uint8_t)_cwState;
}

// Träger für durationMs senden. false, wenn gerade beschäftigt, falscher Modus oder Fehler.
inline bool cwStartTx(uint32_t durationMs) {
  if (!_radioReady || _radioMode != RADIO_MODE_CW || _cwState != CW_IDLE) return false;
  durationMs = constrain(durationMs, (uint32_t)1, (uint32_t)CW_MAX_TX_MS);

  radio.standby();
  int st = radio.transmitDirect();
  if (st != RADIOLIB_ERR_NONE) {
#if RADIO_DEBUG
    Serial.printf("❌ transmitDirect -> %d\n", st);
#endif
    radio.startReceive();
    return false;
  }

  _cwState = CW_TX;
  _cwStartMs = millis();
  _cwDurationMs = durationMs;
#if RADIO_DEBUG
  Serial.printf("📡 CW TX gestartet (%lu ms)\n", (unsigned long)durationMs);
#endif
  _radioDrawUi();
  return true;
}

// RSSI für durationMs messen. false, wenn gerade beschäftigt oder falscher Modus.
inline bool rssiStartMeasure(uint32_t durationMs) {
  if (!_radioReady || _radioMode != RADIO_MODE_CW || _cwState != CW_IDLE) return false;
  durationMs = constrain(durationMs, (uint32_t)1, (uint32_t)CW_MAX_MEASURE_MS);

  _rssiSumMw = 0.0;
  _rssiMin = NAN;   // werden beim ersten Sample gesetzt
  _rssiMax = NAN;
  _rssiCount = 0;

  _cwState = CW_MEASURE;
  _cwStartMs = millis();
  _cwDurationMs = durationMs;
  _rssiNextSampleMs = _cwStartMs;
#if RADIO_DEBUG
  Serial.printf("📏 RSSI-Messung gestartet (%lu ms)\n", (unsigned long)durationMs);
#endif
  _radioDrawUi();
  return true;
}

// CW-Sendung bzw. Messung sofort abbrechen (Messung liefert dann kein Ergebnis)
inline void cwStop() {
  if (_cwState == CW_TX) {
    _cwBackToRx();
#if RADIO_DEBUG
    Serial.println("🛑 CW TX gestoppt");
#endif
  } else if (_cwState == CW_MEASURE) {
#if RADIO_DEBUG
    Serial.println("🛑 RSSI-Messung abgebrochen");
#endif
  }
  _cwState = CW_IDLE;
  _radioDrawUi();
}

// Im CW-Modus im loop() aufrufen
inline void cwTask() {
  uint32_t now = millis();

  if (_cwState == CW_TX) {
    if (now - _cwStartMs >= _cwDurationMs) {
      _cwBackToRx();
      _cwState = CW_IDLE;
#if RADIO_DEBUG
      Serial.println("✅ CW TX beendet");
#endif
      _radioDrawUi();
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

// ============================================================
// Modus und Einstellungen
// ============================================================
inline const RadioSettings& radioSettings() { return _radioCfg; }
inline RadioMode radioMode() { return _radioMode; }
inline bool radioReady() { return _radioReady; }

// Für Register 421: aktueller Modus, oder RADIO_MODE_FAILED
inline uint16_t radioActiveReg() { return _radioReady ? (uint16_t)_radioMode : RADIO_MODE_FAILED; }

// Gewünschten Modus merken; radioTask() schaltet im Leerlauf um
inline void radioRequestMode(RadioMode mode) { _radioWant = mode; }

// Chip für `mode` mit den aktuellen Einstellungen neu initialisieren.
// false: CW läuft noch oder Initialisierung fehlgeschlagen (dann meldet radioActiveReg() RADIO_MODE_FAILED).
inline bool radioSetMode(RadioMode mode) {
  if (_cwState != CW_IDLE) return false;

  _loraIrqEnabled = false;   // keine Paketbehandlung, solange der Chip umkonfiguriert wird
  _loraRxFlag = false;
  _lastMsg = "";             // Duplicate-Filter nicht über einen Moduswechsel hinweg
  radio.standby();

  int st;
  if (mode == RADIO_MODE_LORA) {
    st = radio.begin();
    if (st == RADIOLIB_ERR_NONE) {
      // Heltec V3 hat RF-Switch über DIO2 → wichtig (begin() setzt den Chip zurück)
      radio.setDio2AsRfSwitch(true);
      radio.setFrequency(_radioCfg.freqMHz);
      radio.setOutputPower(_radioCfg.powerDbm);
      radio.setSpreadingFactor(_radioCfg.sf);
      radio.setBandwidth(_radioCfg.bwKhz);
      radio.setCodingRate(_radioCfg.cr);
    }
  } else if (mode == RADIO_MODE_FSK) {
    st = radio.beginFSK(_radioCfg.freqMHz, (float)FSK_BITRATE_KBPS, (float)FSK_FREQ_DEV_KHZ,
                        (float)FSK_RX_BW_KHZ, _radioCfg.powerDbm, (uint16_t)FSK_PREAMBLE_LEN);
    if (st == RADIOLIB_ERR_NONE) {
      radio.setDio2AsRfSwitch(true);
      radio.setDataShaping(RADIOLIB_SHAPING_0_5);   // GFSK, BT = 0.5
      radio.setSyncWord(_fskSyncWord, sizeof(_fskSyncWord));
      radio.setCRC(2);
    }
  } else {
    st = radio.beginFSK(_radioCfg.freqMHz, (float)CW_BITRATE_KBPS, (float)CW_FREQ_DEV_KHZ,
                        (float)CW_RX_BW_KHZ, _radioCfg.powerDbm);
    if (st == RADIOLIB_ERR_NONE) {
      radio.setDio2AsRfSwitch(true);
    }
  }

#if RADIO_DEBUG
  Serial.printf("[Radio] %s -> %d\n",
                mode == RADIO_MODE_LORA ? "begin (LoRa)" :
                mode == RADIO_MODE_FSK  ? "beginFSK (FSK)" : "beginFSK (CW)", st);
#endif
  if (st != RADIOLIB_ERR_NONE) {
    _radioReady = false;
    _radioRetryAtMs = millis() + RADIO_RETRY_MS;
    return false;
  }

  radio.setDio1Action(_onLoraDio1);   // im CW-Modus bleibt _loraIrqEnabled false
  radio.startReceive();
  _loraIrqEnabled = _isPacketMode(mode);

  _radioMode = mode;
  _radioReady = true;

#if RADIO_DEBUG
  if (mode == RADIO_MODE_LORA) {
    Serial.println("⚙️  LoRa Settings:");
    Serial.printf("  Freq   : %.3f MHz\n", _radioCfg.freqMHz);
    Serial.printf("  Power  : %d dBm\n", _radioCfg.powerDbm);
    Serial.printf("  SF     : %d\n", _radioCfg.sf);
    Serial.printf("  BW     : %.0f kHz\n", _radioCfg.bwKhz);
    Serial.printf("  CR     : 4/%d\n", _radioCfg.cr);
  } else if (mode == RADIO_MODE_FSK) {
    Serial.println("⚙️  FSK Settings:");
    Serial.printf("  Freq   : %.3f MHz\n", _radioCfg.freqMHz);
    Serial.printf("  Power  : %d dBm\n", _radioCfg.powerDbm);
    Serial.printf("  Bitrate: %.1f kbps\n", (float)FSK_BITRATE_KBPS);
    Serial.printf("  FreqDev: %.1f kHz\n", (float)FSK_FREQ_DEV_KHZ);
    Serial.printf("  RX BW  : %.1f kHz\n", (float)FSK_RX_BW_KHZ);
  } else {
    Serial.println("⚙️  CW Settings:");
    Serial.printf("  Freq   : %.3f MHz\n", _radioCfg.freqMHz);
    Serial.printf("  Power  : %d dBm\n", _radioCfg.powerDbm);
    Serial.printf("  RX BW  : %.1f kHz\n", (float)CW_RX_BW_KHZ);
  }
#endif

  _radioDrawUi();
  return true;
}

// Einstellungen übernehmen (Frequenz/Leistung in beiden Modi, SF/BW/CR nur im LoRa-Modus;
// im anderen Modus gelten sie ab dem nächsten Wechsel). false, wenn CW läuft – später erneut versuchen.
inline bool radioApplySettings(const RadioSettings& s) {
  if (_cwState != CW_IDLE) return false;
  _radioCfg = s;
  if (!_radioReady) return true;   // wird mit der nächsten Initialisierung übernommen

  _loraIrqEnabled = false;
  radio.standby();
  radio.setFrequency(s.freqMHz);
  radio.setOutputPower(s.powerDbm);
  if (_radioMode == RADIO_MODE_LORA) {
    radio.setSpreadingFactor(s.sf);
    radio.setBandwidth(s.bwKhz);
    radio.setCodingRate(s.cr);
  }
  radio.startReceive();
  _loraIrqEnabled = _isPacketMode(_radioMode);

#if RADIO_DEBUG
  Serial.println("⚙️  Übernehme Radio-Konfiguration:");
  Serial.printf("Frequenz      : %.1f MHz\n", s.freqMHz);
  Serial.printf("Sendeleistung : %d dBm\n", s.powerDbm);
  if (_radioMode == RADIO_MODE_LORA) {
    Serial.printf("Spreading     : SF%d\n", s.sf);
    Serial.printf("Bandwidth     : %.0f kHz\n", s.bwKhz);
    Serial.printf("Coding Rate   : 4/%d\n", s.cr);
  }
#endif
  return true;
}

// In setup(): SPI starten und im LoRa-Modus beginnen (wie die reine Slave-Firmware).
// Scheitert die Initialisierung, wird sie im loop() wiederholt (kein Dauer-Halt).
inline void initRadio() {
  delay(50);
  SPI.begin(LORA_PIN_SCK, LORA_PIN_MISO, LORA_PIN_MOSI, LORA_PIN_NSS);
  radioSetMode(RADIO_MODE_LORA);
}

// Im loop() aufrufen. allowModeSwitch = false z. B. während eines OTA-Updates.
inline void radioTask(bool allowModeSwitch = true) {
  if (allowModeSwitch && (_radioWant != _radioMode || !_radioReady) && _cwState == CW_IDLE &&
      (int32_t)(millis() - _radioRetryAtMs) >= 0) {
    radioSetMode(_radioWant);
  }

  if (!_radioReady) return;
  if (_isPacketMode(_radioMode)) {
    runReceiver();
  } else {
    cwTask();
  }
}

#endif // RADIO_HANDLER_H
