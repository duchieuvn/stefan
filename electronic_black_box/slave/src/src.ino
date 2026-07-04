#include <Arduino.h>
#include <RadioLib.h>

#include "oled_ui.h"
#include "heltec_led.h"
#include "device_id.h"
#include "bme280_handler.h"
#include "modbus_handler.h"
#include "lora_handler.h"

// WICHTIG: nur 1x definieren
SSD1306Wire oled(OLED_I2C_ADDR, OLED_SDA_PIN, OLED_SCL_PIN, OLED_GEOMETRY, I2C_ONE, OLED_I2C_FREQ);

// WICHTIG: radio nur 1x definieren
SX1262 radio = new Module(LORA_PIN_NSS, LORA_PIN_DIO1, LORA_PIN_RST, LORA_PIN_BUSY);

// SLAVE_ID bitte EINMAL global 
const uint8_t SLAVE_ID = 4;


String deviceId;

void setup() {
  Serial.begin(115200);
  deviceId = getMacFull();

  oledInit();
  oledDrawRssiSnr(NAN, NAN, SLAVE_ID);

  setupBme280();
  setupModbus();
  initLoRa();
}

void loop() {
  runReceiver();
  modbusTask();
  loraUiTask(1000);
}
