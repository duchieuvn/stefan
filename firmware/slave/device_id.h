#ifndef DEVICE_ID_H
#define DEVICE_ID_H

#include <Arduino.h>
#include <Preferences.h>
#include <bootloader_random.h>

// Liefert die vollständige MAC-Adresse als 12-stellige Hex-Zeichenkette (ohne Doppelpunkte),
// in normaler Reihenfolge (z. B. 48CA433A6C6C). getEfuseMac() hat MAC-Byte 0 im niedrigsten Byte.
inline String getMacFull() {
  uint64_t mac = ESP.getEfuseMac();
  char full[13];
  sprintf(full, "%02X%02X%02X%02X%02X%02X",
          (uint8_t)(mac),
          (uint8_t)(mac >> 8),
          (uint8_t)(mac >> 16),
          (uint8_t)(mac >> 24),
          (uint8_t)(mac >> 32),
          (uint8_t)(mac >> 40));
  return String(full);
}

// Slave-ID liegt im NVS (Namespace "node") und ueberlebt Neustarts und OTA-Updates.
// Ohne gespeicherte ID wird beim Start eine zufaellige ID 1..247 ausgelost und gespeichert.
inline uint8_t loadSlaveId() {
  Preferences prefs;
  prefs.begin("node", false);
  uint8_t id = prefs.isKey("slave_id") ? prefs.getUChar("slave_id", 0) : 0;
  if (id < 1 || id > 247) {
    // Echte Zufallszahl: ohne WLAN/BT braucht der RNG die Entropiequelle des Bootloaders
    // (nutzt den SAR-ADC, daher vor ADC/WLAN wieder abschalten)
    bootloader_random_enable();
    id = (uint8_t)(esp_random() % 247 + 1);
    bootloader_random_disable();
    prefs.putUChar("slave_id", id);
  }
  prefs.end();
  return id;
}

// id = 0 loescht die gespeicherte ID → beim naechsten Start wird neu ausgelost
inline void saveSlaveId(uint8_t id) {
  Preferences prefs;
  prefs.begin("node", false);
  if (id == 0) prefs.remove("slave_id");
  else prefs.putUChar("slave_id", id);
  prefs.end();
}

#endif
