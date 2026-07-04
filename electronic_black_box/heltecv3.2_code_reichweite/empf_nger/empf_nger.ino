#include <Arduino.h>
#include <Wire.h>
#include <SPI.h>
#include <RadioLib.h>

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

// ---------- LoRa Pins (Heltec V3/V3.2) ----------
#define LORA_NSS   8
#define LORA_SCK   9
#define LORA_MOSI 10
#define LORA_MISO 11
#define LORA_RST  12
#define LORA_BUSY 13
#define LORA_DIO1 14

Module mod(LORA_NSS, LORA_DIO1, LORA_RST, LORA_BUSY);
SX1262 radio(&mod);

// ---------- Funk-Parameter (müssen TX/RX matchen!) ----------
static const float   LORA_FREQ_MHZ   = 433.0;
static const float   LORA_BW_KHZ     = 125.0;
static const int     LORA_SF         = 12;
static const int     LORA_CR         = 8;      // 4/8
static const int     LORA_POWER_DBM  = 22;
static const int     LORA_PREAMBLE   = 16;
static const uint8_t LORA_SYNCWORD   = 0x12;   // Public LoRa
static const uint8_t LORA_CRC_LEN    = 2;

// ---------- Protokoll ----------
static const char* MESSAGE_PING = "Ping_433";

struct __attribute__((packed)) DataPacket {
  char    message[16];
  int16_t rssi;
  int16_t returnedRssi;
};

// ---------- RX Interrupt ----------
volatile bool rxFlag = false;
void rxDoneISR() { rxFlag = true; }

// ---------- Anzeige-Werte ----------
float lastRSSI = NAN, lastSNR = NAN;

// ---------- OLED: RSSI | SNR nebeneinander ----------
void drawRSSISNR() {
  display.clear();
  display.setTextAlignment(TEXT_ALIGN_LEFT);

#ifdef WIRELESS_STICK_V3
  // 64x32: kompakt nebeneinander
  display.setFont(ArialMT_Plain_10);
  display.drawString(0, 0,  "RSSI");
  display.drawString(34, 0, "SNR");

  display.setFont(ArialMT_Plain_16);
  display.drawString(0, 12, isnan(lastRSSI) ? String("---") : String(lastRSSI, 0));
  display.drawString(34, 12, isnan(lastSNR)  ? String("---") : String(lastSNR, 1));

#else
  // 128x64: groß & sauber nebeneinander
  display.setFont(ArialMT_Plain_10);
  display.drawString(0, 0,  "RSSI (dBm)");
  display.drawString(68, 0, "SNR (dB)");
  display.drawLine(64, 0, 64, 63);

  display.setFont(ArialMT_Plain_24);
  String r = isnan(lastRSSI) ? String("---") : String(lastRSSI, 1);
  String s = isnan(lastSNR)  ? String("---") : String(lastSNR, 1);

  display.drawString(0, 18,  r);
  display.drawString(68, 18, s);
#endif

  display.display();
}

void startReceive() {
  radio.setDio1Action(rxDoneISR);
  radio.startReceive(RADIOLIB_SX126X_RX_TIMEOUT_INF);
}

void setupLoRa() {
  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_NSS);

  int st = radio.begin();
  Serial.printf("radio.begin() -> %d\n", st);

  radio.setDio2AsRfSwitch(true);

  // Parameter setzen (müssen TX/RX matchen!)
  radio.setFrequency(LORA_FREQ_MHZ);
  radio.setBandwidth(LORA_BW_KHZ);
  radio.setSpreadingFactor(LORA_SF);
  radio.setCodingRate(LORA_CR);
  radio.setOutputPower(LORA_POWER_DBM);
  radio.setPreambleLength(LORA_PREAMBLE);
  radio.setSyncWord(LORA_SYNCWORD);
  radio.setCRC(LORA_CRC_LEN);

  radio.autoLDRO();
  radio.setRxBoostedGainMode(true, true);

  startReceive();
}

void onDataReceived() {
  DataPacket d{};
  int state = radio.readData((uint8_t*)&d, sizeof(d));

  if (state == RADIOLIB_ERR_NONE) {
    lastRSSI = radio.getRSSI();
    lastSNR  = radio.getSNR();

    Serial.printf("RX OK: '%s' RSSI=%.1f SNR=%.1f\n", d.message, lastRSSI, lastSNR);
    drawRSSISNR();

    // Ping -> Pong
    if (strncmp(d.message, MESSAGE_PING, sizeof(d.message)) == 0) {
      DataPacket pong{};
      strncpy(pong.message, "Pong", sizeof(pong.message) - 1);
      pong.rssi         = (int16_t)lroundf(lastRSSI);
      pong.returnedRssi = pong.rssi;

      int tx = radio.transmit((uint8_t*)&pong, sizeof(pong));
      Serial.printf("TX Pong -> %d\n", tx);

      startReceive();
    }

  } else {
    Serial.printf("RX readData Fehler: %d\n", state);
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);

  VextON();
  delay(100);

  display.init();
  display.clear();
  display.display();
  display.setContrast(255);

  drawRSSISNR();   // zeigt erstmal --- / ---
  setupLoRa();
}

void loop() {
  if (rxFlag) {
    rxFlag = false;
    onDataReceived();
    startReceive();
  }

  // Refresh 1x pro Sekunde (Display bleibt "lebendig")
  static uint32_t lastUi = 0;
  if (millis() - lastUi > 1000) {
    lastUi = millis();
    drawRSSISNR();
  }
}
