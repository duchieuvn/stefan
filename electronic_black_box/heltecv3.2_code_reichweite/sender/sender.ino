// Heltec V3.2 LoRa Sender + OLED (nur RSSI/SNR live)
// Display-Lib: HT_SSD1306Wire (wie bei dir funktionierend)
// Funk: RadioLib (Ping->Pong)

#include <Arduino.h>
#include <SPI.h>
#include <RadioLib.h>

#include <Wire.h>
#include "HT_SSD1306Wire.h"

// ---------- OLED ----------
#ifdef WIRELESS_STICK_V3
static SSD1306Wire display(0x3c, 500000, SDA_OLED, SCL_OLED, GEOMETRY_64_32, RST_OLED);
#else
static SSD1306Wire display(0x3c, 500000, SDA_OLED, SCL_OLED, GEOMETRY_128_64, RST_OLED);
#endif

void VextON() {
  pinMode(Vext, OUTPUT);
  digitalWrite(Vext, LOW);   // Heltec: Vext aktiv LOW
}

// ---------- Heltec V3.2 SX1262 Pins ----------
#define LORA_NSS   8
#define LORA_SCK   9
#define LORA_MOSI 10
#define LORA_MISO 11
#define LORA_RST  12
#define LORA_BUSY 13
#define LORA_DIO1 14

Module mod(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);
SX1262 radio(&mod);

// ---------- Datenpaket ----------
struct __attribute__((packed)) DataPacket {
  char    message[16];
  int16_t rssi;
  int16_t returnedRssi;
};

// ---------- Funk-Settings (müssen TX/RX matchen!) ----------
static const float   LORA_FREQ_MHZ  = 433.0;
static const float   LORA_BW_KHZ    = 125.0;
static const int     LORA_SF        = 12;
static const int     LORA_CR        = 8;      // 4/8
static const int     LORA_PWR_DBM   = 22;
static const int     LORA_PREAMBLE  = 16;
static const uint8_t LORA_SYNCWORD  = 0x12;   // Public LoRa
static const uint8_t LORA_CRC_LEN   = 2;

// ---------- Ping/Pong ----------
static const char* MESSAGE_PING = "Ping_433";
volatile bool rxFlag = false;

unsigned long lastPing = 0;
const uint32_t PING_INTERVAL_MS = 12000;

// ---------- Werte fürs Display ----------
float lastRSSI = NAN;
float lastSNR  = NAN;
uint32_t lastUpdateMs = 0;

// ---------- ISR ----------
void IRAM_ATTR rxDoneISR() { rxFlag = true; }

void drawRSSISNR() {
  display.clear();
  display.setTextAlignment(TEXT_ALIGN_LEFT);

#ifdef WIRELESS_STICK_V3
  // 64x32: kompakt nebeneinander (kleiner)
  display.setFont(ArialMT_Plain_10);
  display.drawString(0, 0, "RSSI");
  display.drawString(34, 0, "SNR");

  display.setFont(ArialMT_Plain_16);
  display.drawString(0, 12, isnan(lastRSSI) ? String("---") : String(lastRSSI, 0));
  display.drawString(34, 12, isnan(lastSNR)  ? String("---") : String(lastSNR, 1));

#else
  // 128x64: RSSI links, SNR rechts (groß), sauber im Rahmen
  display.setFont(ArialMT_Plain_10);
  display.drawString(0, 0,  "RSSI (dBm)");
  display.drawString(68, 0, "SNR (dB)");

  display.setFont(ArialMT_Plain_24);

  // RSSI groß links
  String r = isnan(lastRSSI) ? String("---") : String(lastRSSI, 1);
  display.drawString(0, 18, r);

  // SNR groß rechts (etwas nach rechts eingerückt)
  String s = isnan(lastSNR) ? String("---") : String(lastSNR, 1);
  display.drawString(68, 18, s);

  // optional: kleine Trennlinie
  display.drawLine(64, 0, 64, 63);
#endif

  display.display();
}

// ---------- RX starten ----------
void startReceive() {
  radio.setDio1Action(rxDoneISR);
  radio.startReceive(RADIOLIB_SX126X_RX_TIMEOUT_INF);
}

// ---------- RX verarbeiten ----------
void onDataReceived() {
  DataPacket d{};
  int st = radio.readData((uint8_t*)&d, sizeof(d));
  if (st == RADIOLIB_ERR_NONE) {
    // Wir zeigen einfach immer die aktuellen Funkwerte vom zuletzt empfangenen Paket
    lastRSSI = radio.getRSSI();
    lastSNR  = radio.getSNR();
    lastUpdateMs = millis();

    // optional: nur Pong loggen
    if (strcmp(d.message, "Pong") == 0) {
      Serial.printf("RX Pong | RSSI=%.1f dBm | SNR=%.1f dB\n", lastRSSI, lastSNR);
    }
    drawRSSISNR();
  } else {
    Serial.printf("RX read error: %d\n", st);
  }
}

// ---------- Ping senden ----------
void sendPing() {
  DataPacket d{};
  strncpy(d.message, MESSAGE_PING, sizeof(d.message) - 1);

  int tx = radio.transmit((uint8_t*)&d, sizeof(d));
  if (tx == RADIOLIB_ERR_NONE) Serial.println("TX Ping OK");
  else                         Serial.printf("TX error: %d\n", tx);

  startReceive();
}

// ---------- LoRa Setup ----------
void setupLoRa() {
  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);

  int st = radio.begin();
  Serial.printf("radio.begin -> %d\n", st);

  radio.setDio2AsRfSwitch(true);

  radio.setFrequency(LORA_FREQ_MHZ);
  radio.setBandwidth(LORA_BW_KHZ);
  radio.setSpreadingFactor(LORA_SF);
  radio.setCodingRate(LORA_CR);
  radio.setOutputPower(LORA_PWR_DBM);
  radio.setPreambleLength(LORA_PREAMBLE);
  radio.setSyncWord(LORA_SYNCWORD);
  radio.setCRC(LORA_CRC_LEN);

  radio.autoLDRO();
  radio.setRxBoostedGainMode(true, true);

  startReceive();
}

// ---------- Arduino ----------
void setup() {
  Serial.begin(115200);
  delay(100);

  VextON();
  delay(100);

  display.init();
  display.clear();
  display.display();
  display.setContrast(255);

  drawRSSISNR();

  setupLoRa();
}

void loop() {
  if (rxFlag) {
    rxFlag = false;
    onDataReceived();
    startReceive();
  }

  if (millis() - lastPing >= PING_INTERVAL_MS) {
    lastPing = millis();
    sendPing();
  }

  // Refresh 1x pro Sekunde (zeigt auch dann was, wenn gerade nichts kommt)
  static uint32_t lastUi = 0;
  if (millis() - lastUi > 1000) {
    lastUi = millis();
    drawRSSISNR();
  }
}
