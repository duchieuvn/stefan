#pragma once
#include <Arduino.h>

// Heltec WiFi LoRa 32 V3: weiße User-LED
static constexpr uint8_t HELTEC_LED_PIN = 35;

// PWM settings
static constexpr uint32_t HELTEC_LED_PWM_FREQ = 5000; // Hz
static constexpr uint8_t  HELTEC_LED_PWM_RES  = 8;    // 8-bit (0..255)

inline void heltec_led(uint8_t percent) {
  if (percent > 100) percent = 100;

  static bool inited = false;
  static int  channel = 0; // nur für "alten" LEDC-API Pfad

  if (!inited) {
    pinMode(HELTEC_LED_PIN, OUTPUT);

    // Arduino-ESP32 Core 3.x: ledcAttach(pin,freq,res), ledcWrite(pin,duty)
    #if __has_include("esp_arduino_version.h")
      #include "esp_arduino_version.h"
    #endif

    #if defined(ESP_ARDUINO_VERSION) && (ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0))
      ledcAttach(HELTEC_LED_PIN, HELTEC_LED_PWM_FREQ, HELTEC_LED_PWM_RES);
    #else
      // Arduino-ESP32 Core 2.x: ledcSetup/ledcAttachPin/ledcWrite(channel,duty)
      ledcSetup(channel, HELTEC_LED_PWM_FREQ, HELTEC_LED_PWM_RES);
      ledcAttachPin(HELTEC_LED_PIN, channel);
    #endif

    inited = true;
  }

  const uint32_t maxDuty = (1u << HELTEC_LED_PWM_RES) - 1u;
  const uint32_t duty = (percent * maxDuty) / 100u;

  #if defined(ESP_ARDUINO_VERSION) && (ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0))
    ledcWrite(HELTEC_LED_PIN, duty);
  #else
    ledcWrite(channel, duty);
  #endif
}
