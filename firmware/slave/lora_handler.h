#ifndef LORA_HANDLER_H
#define LORA_HANDLER_H

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>
#include "oled_ui.h"

// OLED-Refresh (1 Hz), damit Anzeige „lebt“
static uint32_t _lastUiMs = 0;

// ============================================================
// Heltec WiFi LoRa 32 V3.x (SX1262) – Default Pinout
// (kannst du vor dem Include mit #define ... überschreiben)
// ============================================================
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

// Optional: Status-LED (falls du eine hast). -1 = deaktiviert
#ifndef LORA_STATUS_LED_PIN
  #define LORA_STATUS_LED_PIN (-1)
#endif

// Default: IRQ/non-blocking RX aktiv (empfohlen, damit Modbus nicht blockiert)
#ifndef LORA_USE_IRQ_RX
  #define LORA_USE_IRQ_RX 1
#endif

// Debug-Ausgaben
#ifndef LORA_DEBUG
  #define LORA_DEBUG 1
#endif

// Standard-LoRa-Parameter (kannst du überschreiben)
#ifndef LORA_FREQ_MHZ
  #define LORA_FREQ_MHZ 433.0f
#endif
#ifndef LORA_TX_POWER_DBM
  #define LORA_TX_POWER_DBM 22
#endif
#ifndef LORA_SF
  #define LORA_SF 7
#endif
#ifndef LORA_BW_KHZ
  #define LORA_BW_KHZ 125.0f
#endif
#ifndef LORA_CR
  #define LORA_CR 5   // 5..8 entspricht 4/5..4/8
#endif

// ============================================================
// Externe Dinge (werden woanders definiert)
// ============================================================

// WICHTIG: radio darf genau 1x in einer .ino/.cpp Datei instanziiert werden!
// Beispiel (in deiner .ino):
//   SX1262 radio = new Module(LORA_PIN_NSS, LORA_PIN_DIO1, LORA_PIN_RST, LORA_PIN_BUSY);
extern SX1262 radio;

extern String deviceId;
extern uint8_t SLAVE_ID;  // aus NVS, siehe device_id.h

// Diese Funktion kommt aus modbus_handler.* (oder wo du sie implementierst)
void updateLoRaInfo(const String& idStr, uint32_t timestamp, float rssi, float snr);

// ============================================================
// Interner Zustand (static, damit Header-Only ohne Mehrfachdef.)
// ============================================================
static String _lastMsg = "";
static float  _lastRSSI = 0.0f;
static float  _lastSNR  = 0.0f;
static String _lastPayload = "";

#if LORA_USE_IRQ_RX
static volatile bool _loraRxFlag = false;
static volatile bool _loraIrqEnabled = true;

// RadioLib ruft diese ISR bei DIO1-IRQ auf
static void IRAM_ATTR _onLoraDio1() {
  if (!_loraIrqEnabled) return;
  _loraRxFlag = true;
}
#endif

// ============================================================
// Hilfsfunktionen
// ============================================================
static inline void _loraLedPulse(uint16_t ms = 25) {
#if (LORA_STATUS_LED_PIN >= 0)
  digitalWrite(LORA_STATUS_LED_PIN, HIGH);
  delay(ms);
  digitalWrite(LORA_STATUS_LED_PIN, LOW);
#else
  (void)ms;
#endif
}

static inline void _loraPrintParams() {
#if LORA_DEBUG
  Serial.println("⚙️  LoRa Settings:");
  Serial.printf("  Freq   : %.3f MHz\n", (float)LORA_FREQ_MHZ);
  Serial.printf("  Power  : %d dBm\n", (int)LORA_TX_POWER_DBM);
  Serial.printf("  SF     : %d\n", (int)LORA_SF);
  Serial.printf("  BW     : %.0f kHz\n", (float)LORA_BW_KHZ);
  Serial.printf("  CR     : 4/%d\n", (int)LORA_CR);
#endif
}

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

  // Wie bisher: dein Modbus-Update (Payload ignorieren ist ok)
  updateLoRaInfo(idStr, ts, rssi, snr);

#if LORA_DEBUG
  Serial.println("---- Neue LoRa-Nachricht ----");
  Serial.printf("Absender ID : %s\n", idStr.c_str());
  Serial.printf("Zeitstempel : %lu ms\n", (unsigned long)ts);
  Serial.printf("RSSI        : %.1f dBm\n", rssi);
  Serial.printf("SNR         : %.1f dB\n", snr);
  Serial.printf("Payload     : '%s'\n", payload.c_str()); // leer => ''
  Serial.println("-----------------------------");
#endif

  _loraLedPulse(25);
  oledDrawRssiSnr(_lastRSSI, _lastSNR, SLAVE_ID);
}

// ============================================================
// Public API (Header-only)
// ============================================================

inline void initLoRa() {
#if (LORA_STATUS_LED_PIN >= 0)
  pinMode(LORA_STATUS_LED_PIN, OUTPUT);
  digitalWrite(LORA_STATUS_LED_PIN, LOW);
#endif

  delay(50);

#if LORA_DEBUG
  Serial.println("[LoRa] SPI.begin(...)");
#endif
  SPI.begin(LORA_PIN_SCK, LORA_PIN_MISO, LORA_PIN_MOSI, LORA_PIN_NSS);

#if LORA_DEBUG
  Serial.println("[LoRa] radio.begin()…");
#endif
  int st = radio.begin();
#if LORA_DEBUG
  Serial.printf("[LoRa] begin -> %d\n", st);
#endif
  if (st != RADIOLIB_ERR_NONE) {
#if LORA_DEBUG
    Serial.println("❌ SX1262 Fehler bei begin()");
#endif
    while (true) { delay(100); }
  }

  // Heltec V3 hat RF-Switch über DIO2 → wichtig
  radio.setDio2AsRfSwitch(true);

  // Parameter setzen (Fehlercodes ignorieren wir hier kurz – kannst du gerne prüfen)
  radio.setFrequency((float)LORA_FREQ_MHZ);
  radio.setOutputPower((int)LORA_TX_POWER_DBM);
  radio.setSpreadingFactor((int)LORA_SF);
  radio.setBandwidth((float)LORA_BW_KHZ);
  radio.setCodingRate((int)LORA_CR);

  _loraPrintParams();

#if LORA_USE_IRQ_RX
  // Non-blocking RX per IRQ
  radio.setDio1Action(_onLoraDio1);
  radio.startReceive();
#if LORA_DEBUG
  Serial.println("[LoRa] IRQ RX gestartet (startReceive)");
#endif
#else
  // Für blocking receive() später
  radio.startReceive();
#if LORA_DEBUG
  Serial.println("[LoRa] RX gestartet (startReceive, blocking mode möglich)");
#endif
#endif
}

// Non-blocking (IRQ) oder blocking (fallback) – gleicher API-Name wie bei dir
inline void runReceiver() {
#if LORA_USE_IRQ_RX
  if (!_loraRxFlag) return;

  _loraIrqEnabled = false;
  _loraRxFlag = false;

  String msg;
  int state = radio.readData(msg);

  if (state == RADIOLIB_ERR_NONE) {
    float rssi = radio.getRSSI();
    float snr  = radio.getSNR();
    _handleNewMessage(msg, rssi, snr);
  } else {
#if LORA_DEBUG
    // RX_TIMEOUT kann je nach Timing auftreten; wir loggen nur “echte” Fehler gern optional
    // Serial.printf("[LoRa] readData state=%d\n", state);
#endif
  }

  radio.startReceive();
  _loraIrqEnabled = true;

#else
  // Blocking-Version (nicht empfohlen bei Modbus/RS485, aber kompatibel)
  String msg;
  int state = radio.receive(msg);
  if (state == RADIOLIB_ERR_NONE) {
    float rssi = radio.getRSSI();
    float snr  = radio.getSNR();
    _handleNewMessage(msg, rssi, snr);
  }
  radio.startReceive();
#endif
}

inline void loraUiTask(uint32_t periodMs = 1000) {
  uint32_t now = millis();
  if (now - _lastUiMs < periodMs) return;
  _lastUiMs = now;

  // auch ohne neue Pakete: Werte erneut zeichnen
  oledDrawRssiSnr(_lastRSSI, _lastSNR, SLAVE_ID);
}

#endif // LORA_HANDLER_H
