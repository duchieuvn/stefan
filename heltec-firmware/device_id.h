#ifndef DEVICE_ID_H
#define DEVICE_ID_H

#include <Arduino.h>

// Liefert die vollständige MAC-Adresse als 12-stellige Hex-Zeichenkette (ohne Doppelpunkte)
inline String getMacFull() {
  uint64_t mac = ESP.getEfuseMac();
  char full[13];
  sprintf(full, "%02X%02X%02X%02X%02X%02X",
          (uint8_t)(mac >> 40),
          (uint8_t)(mac >> 32),
          (uint8_t)(mac >> 24),
          (uint8_t)(mac >> 16),
          (uint8_t)(mac >> 8),
          (uint8_t)(mac));
  return String(full);
}

#endif
