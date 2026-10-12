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

// CW-Anzeige: links RSSI-Mittelwert, rechts RSSI-Maximum der letzten Messung,
// Footer: Slave-ID + aktueller Zustand (IDLE / CW TX / MESSUNG)
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
  oled.drawString(64, 54, "ID " + String(slaveId) + " | " + String(state));
  oled.setTextAlignment(TEXT_ALIGN_LEFT);

  oled.display();
}
