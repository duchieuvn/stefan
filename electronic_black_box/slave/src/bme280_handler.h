#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include "modbus_handler.h"   // für mb + Register-Konstanten

// I2C-Pins für BME280 am Heltec V3
#define BME_SDA 19
#define BME_SCL 20

// eigener I2C-Bus nur für den BME (Controller 1)
static TwoWire BMEWire = TwoWire(1);

// BME280-Objekt & Status
static Adafruit_BME280 bme;
static bool bmeOk = false;

// Initialisierung des BME280
inline void setupBme280() {
  // NICHT Wire.begin() anfassen – der gehört Heltec/OLED!
  BMEWire.begin(BME_SDA, BME_SCL);

  // BME280 initialisieren (Adresse ggf. auf 0x77 ändern)
  bool status = bme.begin(0x76, &BMEWire);
  if (!status) {
    Serial.println("BME280 nicht gefunden (Adresse 0x76?)");
    bmeOk = false;
  } else {
    Serial.println("BME280 gefunden");
    bmeOk = true;
  }
}

/**
 * Führt genau EINE BME-Messung aus und schreibt die Werte
 * in die entsprechenden Modbus-Register.
 * 
 * Diese Funktion wird NICHT automatisch aufgerufen.
 * Du kannst sie später an beliebiger Stelle verwenden:
 *   measureBme280();
 */
inline void measureBme280() {
  if (!bmeOk) {
    Serial.println("BME280 nicht bereit – Messung abgebrochen");
    return;
  }

  float t = bme.readTemperature();       // °C
  float h = bme.readHumidity();          // % rF
  float p = bme.readPressure() / 100.0F; // hPa (BME liefert Pa)

  // einfache Plausibilitätschecks
  if (isnan(t) || isnan(h) || isnan(p)) {
    Serial.println("BME280 Messfehler");
    return;
  }

  // In Modbus-Register schreiben (Skalierung: T/H x100, P x10)
  mb.Hreg(TEMP_REGISTER,  (int16_t)round(t * 100.0f));   // °C x100, signed
  mb.Hreg(HUM_REGISTER,   (uint16_t)round(h * 100.0f));  // % x100
  mb.Hreg(PRESS_REGISTER, (uint16_t)round(p * 10.0f));   // hPa x10

  // Debug-Ausgabe
  Serial.print("BME280 -> T: ");
  Serial.print(t);
  Serial.print(" °C  H: ");
  Serial.print(h);
  Serial.print(" %  P: ");
  Serial.print(p);
  Serial.println(" hPa");
}
