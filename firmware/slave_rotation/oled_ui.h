#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <SSD1306Wire.h>

// ===== Heltec WiFi LoRa 32 V3.x OLED Pins =====
#ifndef OLED_I2C_ADDR
  #define OLED_I2C_ADDR 0x3C
#endif
#ifndef OLED_SDA_PIN
  #define OLED_SDA_PIN 17
#endif
#ifndef OLED_SCL_PIN
  #define OLED_SCL_PIN 18
#endif
#ifndef OLED_RST_PIN
  #define OLED_RST_PIN 21
#endif
#ifndef OLED_GEOMETRY
  #define OLED_GEOMETRY GEOMETRY_128_64
#endif
#ifndef OLED_I2C_FREQ
  #define OLED_I2C_FREQ 500000L
#endif
#ifndef VEXT_CTRL_PIN
  #define VEXT_CTRL_PIN 36     // Heltec: Vext aktiv LOW
#endif

// Display wird GENAU 1x in der .ino definiert
extern SSD1306Wire oled;
extern String deviceId;  // MAC, wird im Setup gesetzt (getMacFull())

inline void oledVextOn() {
  pinMode(VEXT_CTRL_PIN, OUTPUT);
  digitalWrite(VEXT_CTRL_PIN, LOW);
}

inline void oledHardReset() {
  pinMode(OLED_RST_PIN, OUTPUT);
  digitalWrite(OLED_RST_PIN, LOW);
  delay(10);
  digitalWrite(OLED_RST_PIN, HIGH);
  delay(10);
}

inline void oledInit() {
  oledVextOn();
  delay(80);

  oledHardReset();

  Wire.begin(OLED_SDA_PIN, OLED_SCL_PIN);
  Wire.setClock(OLED_I2C_FREQ);

  oled.init();
  oled.clear();
  oled.setContrast(255);
  oled.display();
}

// Fusszeile: Linie + "ID: 11 - 48CA433A6C6C" (max. ~120 px breit)
inline void oledDrawFooter(uint8_t slaveId) {
  oled.drawLine(0, 52, 127, 52);
  oled.setFont(ArialMT_Plain_10);
  oled.setTextAlignment(TEXT_ALIGN_CENTER);
  oled.drawString(64, 54, "ID: " + String(slaveId) + " - " + deviceId);
  oled.setTextAlignment(TEXT_ALIGN_LEFT);
}

inline void oledDrawRssiSnr(float rssi, float snr, uint8_t slaveId) {
  oled.clear();
  oled.setTextAlignment(TEXT_ALIGN_LEFT);

  // 128x64 (wie dein "schön am ganzen Display"-Beispiel + Footer)
  const int splitX = 64;

  // Labels oben (wie im Beispiel)
  oled.setFont(ArialMT_Plain_10);
  oled.drawString(0,  0, "RSSI (dBm)");
  oled.drawString(68, 0, "SNR (dB)");

  // Trennlinie (nur bis über den Footer, damit unten frei ist)
  oled.drawLine(splitX, 0, splitX, 52);

  // Große Werte (genau wie im Beispiel)
  oled.setFont(ArialMT_Plain_24);
  String r = isnan(rssi) ? String("---") : String(rssi, 1);
  String s = isnan(snr)  ? String("---") : String(snr, 1);

  oled.drawString(0,  18, r);
  oled.drawString(68, 18, s);

  // Footer-Linie + Slave-ID und MAC in der letzten Zeile
  oledDrawFooter(slaveId);


  oled.display();
}

// CW-Anzeige: links RSSI-Mittelwert, rechts RSSI-Maximum der letzten Messung,
// Footer: Slave-ID + Betriebsart + aktueller Zustand (IDLE / TX / MESSUNG)
inline void oledDrawCw(float rssiAvg, float rssiMax, const char* state, uint8_t slaveId) {
  oled.clear();
  oled.setTextAlignment(TEXT_ALIGN_LEFT);

  const int splitX = 64;

  oled.setFont(ArialMT_Plain_10);
  oled.drawString(0,  0, "RSSI avg");
  oled.drawString(68, 0, "RSSI max");

  oled.drawLine(splitX, 0, splitX, 52);

  oled.setFont(ArialMT_Plain_24);
  String a = isnan(rssiAvg) ? String("---") : String(rssiAvg, 1);
  String m = isnan(rssiMax) ? String("---") : String(rssiMax, 1);

  oled.drawString(0,  18, a);
  oled.drawString(68, 18, m);

  oled.drawLine(0, 52, 127, 52);
  oled.setFont(ArialMT_Plain_10);
  oled.setTextAlignment(TEXT_ALIGN_CENTER);
  oled.drawString(64, 54, "ID " + String(slaveId) + " | CW " + String(state));
  oled.setTextAlignment(TEXT_ALIGN_LEFT);

  oled.display();
}

// Anzeige während eines OTA-Updates (Status-Codes siehe ota_handler.h)
inline void oledDrawOta(uint16_t status, uint16_t progress, uint8_t slaveId) {
  const char* text = status == 1 ? "WLAN ..." : status == 2 ? "Download" : status == 3 ? "Pruefe ..." :
                     status == 4 ? "Neustart" : "Fehler";

  oled.clear();
  oled.setTextAlignment(TEXT_ALIGN_LEFT);
  oled.setFont(ArialMT_Plain_10);
  oled.drawString(0, 0, "Firmware-Update");

  oled.setFont(ArialMT_Plain_24);
  oled.drawString(0, 14, status == 2 ? String(progress) + " %" : String(text));
  oled.drawProgressBar(0, 42, 127, 8, progress > 100 ? 100 : progress);

  oledDrawFooter(slaveId);

  oled.display();
}

