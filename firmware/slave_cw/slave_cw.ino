#include <Arduino.h>
#include <RadioLib.h>

#include "oled_ui.h"
#include "heltec_led.h"
#include "device_id.h"
#include "cw_handler.h"
#include "modbus_handler.h"

// WICHTIG: nur 1x definieren
SSD1306Wire oled(OLED_I2C_ADDR, OLED_SDA_PIN, OLED_SCL_PIN, OLED_GEOMETRY, I2C_ONE, OLED_I2C_FREQ);

// WICHTIG: radio nur 1x definieren
SX1262 radio = new Module(LORA_PIN_NSS, LORA_PIN_DIO1, LORA_PIN_RST, LORA_PIN_BUSY);

// SLAVE_ID bitte EINMAL global
const uint8_t SLAVE_ID = 10;


String deviceId;

void setup() {
  Serial.begin(115200);
  deviceId = getMacFull();

  oledInit();
  oledDrawCw(NAN, NAN, "IDLE", SLAVE_ID);

  setupModbus();
  initRadio();
}

void loop() {
  modbusTask();
  cwTask();
  cwUiTask(1000);
}
