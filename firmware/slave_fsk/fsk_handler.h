#ifndef FSK_HANDLER_H
#define FSK_HANDLER_H

#include <Arduino.h>
#include <RadioLib.h>

// ============================================================
// GFSK-Variante der Radio-Hooks für lora_handler.h
// Wird NUR aus lora_handler.h eingebunden (bei RADIO_USE_FSK = 1),
// nachdem LORA_FREQ_MHZ, LORA_TX_POWER_DBM und LORA_DEBUG definiert sind.
// ============================================================

extern SX1262 radio;

// Standard-FSK-Parameter (Frequenz/Leistung kommen von LORA_FREQ_MHZ / LORA_TX_POWER_DBM)
#ifndef FSK_BITRATE_KBPS
  #define FSK_BITRATE_KBPS 4.8f
#endif
#ifndef FSK_FREQ_DEV_KHZ
  #define FSK_FREQ_DEV_KHZ 5.0f
#endif
#ifndef FSK_RX_BW_KHZ
  #define FSK_RX_BW_KHZ 39.0f   // muss ein SX126x-Wert sein (4.8, 5.8, 7.3, ... 39.0, 46.9, ... 467.0)
#endif
#ifndef FSK_PREAMBLE_LEN
  #define FSK_PREAMBLE_LEN 16   // in Bits
#endif

static uint8_t _fskSyncWord[] = {0x2D, 0xD4};

static inline int _radioBegin() {
#if LORA_DEBUG
  Serial.println("[LoRa] radio.beginFSK()…");
#endif
  int st = radio.beginFSK((float)LORA_FREQ_MHZ, (float)FSK_BITRATE_KBPS, (float)FSK_FREQ_DEV_KHZ,
                          (float)FSK_RX_BW_KHZ, (int)LORA_TX_POWER_DBM, (uint16_t)FSK_PREAMBLE_LEN);
#if LORA_DEBUG
  Serial.printf("[LoRa] beginFSK -> %d\n", st);
#endif
  return st;
}

static inline void _radioConfigure() {
  // Freq/Power/Bitrate/Dev/RX-BW/Preamble sind schon über beginFSK() gesetzt
  radio.setDataShaping(RADIOLIB_SHAPING_0_5);   // GFSK, BT = 0.5
  radio.setSyncWord(_fskSyncWord, sizeof(_fskSyncWord));
  radio.setCRC(2);
}

static inline void _radioPrintParams() {
#if LORA_DEBUG
  Serial.println("⚙️  Radio Settings:");
  Serial.println("  Mode   : FSK");
  Serial.printf("  Freq   : %.3f MHz\n", (float)LORA_FREQ_MHZ);
  Serial.printf("  Power  : %d dBm\n", (int)LORA_TX_POWER_DBM);
  Serial.printf("  Bitrate: %.1f kbps\n", (float)FSK_BITRATE_KBPS);
  Serial.printf("  FreqDev: %.1f kHz\n", (float)FSK_FREQ_DEV_KHZ);
  Serial.printf("  RX BW  : %.1f kHz\n", (float)FSK_RX_BW_KHZ);
#endif
}

// FSK liefert kein SNR (getSNR() gibt dort einen Fehlercode zurück) → NAN
static inline float _radioReadSnr() {
  return NAN;
}

#endif // FSK_HANDLER_H
