#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Update.h>
#include <Preferences.h>
#include <ModbusRTU.h>
#include "oled_ui.h"

// ============================================================
// OTA-Update über WLAN, gesteuert per Modbus vom Master
// ============================================================
// Ablauf:
//   1. Master schreibt SSID, Passwort, URL (und optional MD5) in die Register unten
//   2. Master schreibt CMD 50 → Knoten verbindet sich mit dem WLAN, lädt die .bin
//      (GitHub-Release, Redirects werden verfolgt), flasht sie und startet neu
//   3. Master liest nach dem Neustart das Ergebnis (503)
// Der Download läuft in einem eigenen Task, damit Modbus währenddessen antwortet.

extern ModbusRTU mb;
extern uint8_t SLAVE_ID;  // aus NVS, siehe device_id.h

// ------------------------------
// 📦 Register
// ------------------------------
const uint16_t OTA_STATUS_REG      = 500;  // siehe OTA_ST_* unten
const uint16_t OTA_PROGRESS_REG    = 501;  // 0..100 %
const uint16_t OTA_DETAIL_REG      = 502;  // HTTP-Code bzw. Update-Fehler
const uint16_t OTA_LAST_RESULT_REG = 503;  // Status des letzten Updates (NVS, überlebt Neustart)
const uint16_t OTA_LAST_DETAIL_REG = 504;  // Detail des letzten Updates (NVS)

// Strings: Längenregister, danach 2 ASCII-Bytes pro Register (High-Byte zuerst)
const uint16_t OTA_SSID_LEN_REG    = 510;
const uint16_t OTA_SSID_BASE       = 511;
const uint16_t OTA_SSID_MAX        = 32;
const uint16_t OTA_PASS_LEN_REG    = 530;
const uint16_t OTA_PASS_BASE       = 531;
const uint16_t OTA_PASS_MAX        = 64;
const uint16_t OTA_URL_LEN_REG     = 570;
const uint16_t OTA_URL_BASE        = 571;
const uint16_t OTA_URL_MAX         = 160;
const uint16_t OTA_MD5_BASE        = 660;  // 32 Hex-Zeichen, alles 0 = keine Prüfung
const uint16_t OTA_MD5_LEN         = 32;

// CMD-Register (200): 50 = OTA starten, 51 = OTA abbrechen
const uint16_t CMD_OTA_START       = 50;
const uint16_t CMD_OTA_ABORT       = 51;

// Status-Codes
const uint16_t OTA_ST_IDLE         = 0;
const uint16_t OTA_ST_WIFI         = 1;   // verbinde mit WLAN
const uint16_t OTA_ST_DOWNLOAD     = 2;   // lade & schreibe
const uint16_t OTA_ST_FINALIZE     = 3;   // prüfe Image
const uint16_t OTA_ST_SUCCESS      = 4;   // fertig, Neustart folgt
const uint16_t OTA_ST_ERR_WIFI     = 20;
const uint16_t OTA_ST_ERR_HTTP     = 21;
const uint16_t OTA_ST_ERR_BEGIN    = 22;  // kein Platz / keine OTA-Partition
const uint16_t OTA_ST_ERR_WRITE    = 23;  // Schreib- oder Stream-Fehler
const uint16_t OTA_ST_ERR_VERIFY   = 24;  // Image- oder MD5-Prüfung fehlgeschlagen
const uint16_t OTA_ST_ERR_ABORTED  = 25;
const uint16_t OTA_ST_ERR_PARAMS   = 26;
const uint16_t OTA_ST_STARTED      = 1;   // nur in NVS: Update lief, als der Knoten neu startete

#ifndef OTA_WIFI_TIMEOUT_MS
  #define OTA_WIFI_TIMEOUT_MS 20000
#endif
#ifndef OTA_STALL_TIMEOUT_MS
  #define OTA_STALL_TIMEOUT_MS 20000
#endif

// ------------------------------
// Zustand (Task schreibt nur diese Variablen; loop() spiegelt sie in die Register)
// ------------------------------
static volatile uint16_t _otaStatus   = OTA_ST_IDLE;
static volatile uint16_t _otaProgress = 0;
static volatile uint16_t _otaDetail   = 0;
static volatile bool     _otaRunning  = false;
static volatile bool     _otaAbort    = false;

static char _otaSsid[OTA_SSID_MAX + 1];
static char _otaPass[OTA_PASS_MAX + 1];
static char _otaUrl[OTA_URL_MAX + 1];
static char _otaMd5[OTA_MD5_LEN + 1];
static uint8_t _otaBuf[2048];
static uint32_t _otaLastUiMs = 0;

// ------------------------------
// Hilfsfunktionen
// ------------------------------
static void _otaSaveResult(uint16_t status, uint16_t detail) {
  Preferences prefs;
  prefs.begin("ota", false);
  prefs.putUShort("last", status);
  prefs.putUShort("detail", detail);
  prefs.end();
}

// Liest einen String aus den Registern (Länge in lenReg) und nullt die Register danach.
static void _otaTakeString(uint16_t lenReg, uint16_t base, uint16_t maxBytes, char* out) {
  uint16_t len = mb.Hreg(lenReg);
  if (len > maxBytes) len = maxBytes;
  for (uint16_t i = 0; i < len; i++) {
    uint16_t w = mb.Hreg(base + i / 2);
    out[i] = (char)((i % 2 == 0) ? (w >> 8) : (w & 0xFF));
  }
  out[len] = '\0';
  mb.Hreg(lenReg, 0);
  for (uint16_t i = 0; i < (maxBytes + 1) / 2; i++) mb.Hreg(base + i, 0);
}

static void _otaWipeSecrets() {
  memset(_otaSsid, 0, sizeof(_otaSsid));
  memset(_otaPass, 0, sizeof(_otaPass));
}

static void _otaWifiOff() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
}

// ------------------------------
// OTA-Task
// ------------------------------
// Lädt und flasht das Image; gibt OTA_ST_SUCCESS oder einen Fehlerstatus zurück
// (detail = HTTP-Code bzw. Update-Fehler). Kehrt immer zurück, damit die
// Destruktoren von HTTP- und TLS-Client laufen (sonst bleibt deren Speicher belegt).
static uint16_t _otaDownloadAndFlash(uint16_t& detail) {
  detail = 0;

  // 1) WLAN
  _otaStatus = OTA_ST_WIFI;
  WiFi.mode(WIFI_STA);
  WiFi.begin(_otaSsid, _otaPass[0] ? _otaPass : nullptr);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (_otaAbort) return OTA_ST_ERR_ABORTED;
    if (millis() - t0 > OTA_WIFI_TIMEOUT_MS) {
      detail = (uint16_t)WiFi.status();
      return OTA_ST_ERR_WIFI;
    }
    vTaskDelay(pdMS_TO_TICKS(200));
  }
  Serial.printf("📶 WLAN verbunden, IP %s\n", WiFi.localIP().toString().c_str());

  // 2) HTTP(S)-Download. Kein Zertifikat-Check: die Echtheit sichert der MD5-Wert,
  //    den der Master über eine geprüfte TLS-Verbindung berechnet hat.
  _otaStatus = OTA_ST_DOWNLOAD;
  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.useHTTP10(true);  // kein Chunked-Encoding → Stream ist die reine .bin
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);  // GitHub antwortet mit 302
  http.setConnectTimeout(15000);
  http.setTimeout(15000);
  if (!http.begin(client, _otaUrl)) return OTA_ST_ERR_PARAMS;

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    detail = (uint16_t)(code < 0 ? 0 : code);
    return OTA_ST_ERR_HTTP;
  }
  int total = http.getSize();
  if (total <= 0) return OTA_ST_ERR_HTTP;
  if (!Update.begin((size_t)total)) {
    detail = (uint16_t)Update.getError();
    return OTA_ST_ERR_BEGIN;
  }
  if (_otaMd5[0]) Update.setMD5(_otaMd5);
  Serial.printf("⬇️  Lade %d Bytes\n", total);

  auto* stream = http.getStreamPtr();
  size_t written = 0;
  uint32_t lastData = millis();
  while (written < (size_t)total) {
    if (_otaAbort) {
      Update.abort();
      return OTA_ST_ERR_ABORTED;
    }
    size_t avail = stream->available();
    if (avail) {
      size_t want = min(avail, min(sizeof(_otaBuf), (size_t)total - written));
      size_t n = stream->readBytes(_otaBuf, want);
      if (n > 0) {
        if (Update.write(_otaBuf, n) != n) {
          detail = (uint16_t)Update.getError();
          Update.abort();
          return OTA_ST_ERR_WRITE;
        }
        written += n;
        _otaProgress = (uint16_t)((uint64_t)written * 100 / total);
        lastData = millis();
      }
    } else if (millis() - lastData > OTA_STALL_TIMEOUT_MS) {
      Update.abort();
      return OTA_ST_ERR_WRITE;
    } else {
      vTaskDelay(pdMS_TO_TICKS(5));
    }
  }

  // 3) Image prüfen (Prüfsumme, ggf. MD5) und als Boot-Partition setzen
  _otaStatus = OTA_ST_FINALIZE;
  if (!Update.end(true)) {
    detail = (uint16_t)Update.getError();
    return OTA_ST_ERR_VERIFY;
  }
  return OTA_ST_SUCCESS;
}

static void _otaTask(void*) {
  uint16_t detail = 0;
  uint16_t status = _otaDownloadAndFlash(detail);

  _otaWipeSecrets();
  _otaWifiOff();
  _otaSaveResult(status, detail);
  _otaDetail = detail;
  _otaStatus = status;

  if (status == OTA_ST_SUCCESS) {
    Serial.println("✅ OTA fertig, Neustart ...");
    vTaskDelay(pdMS_TO_TICKS(1500));  // Master soll Status 4 noch lesen können
    ESP.restart();
  }
  Serial.printf("❌ OTA fehlgeschlagen: Status %u, Detail %u\n", status, detail);
  _otaRunning = false;
  vTaskDelete(nullptr);
}

// ------------------------------
// Public API
// ------------------------------
inline bool otaActive() { return _otaRunning; }

// Aus setupModbus() aufrufen
inline void otaSetupRegisters() {
  Preferences prefs;
  prefs.begin("ota", true);
  uint16_t last = prefs.getUShort("last", OTA_ST_IDLE);
  uint16_t detail = prefs.getUShort("detail", 0);
  prefs.end();

  mb.addHreg(OTA_STATUS_REG, OTA_ST_IDLE);
  mb.addHreg(OTA_PROGRESS_REG, 0);
  mb.addHreg(OTA_DETAIL_REG, 0);
  mb.addHreg(OTA_LAST_RESULT_REG, last);
  mb.addHreg(OTA_LAST_DETAIL_REG, detail);

  mb.addHreg(OTA_SSID_LEN_REG, 0);
  mb.addHreg(OTA_SSID_BASE, 0, (OTA_SSID_MAX + 1) / 2);
  mb.addHreg(OTA_PASS_LEN_REG, 0);
  mb.addHreg(OTA_PASS_BASE, 0, (OTA_PASS_MAX + 1) / 2);
  mb.addHreg(OTA_URL_LEN_REG, 0);
  mb.addHreg(OTA_URL_BASE, 0, (OTA_URL_MAX + 1) / 2);
  mb.addHreg(OTA_MD5_BASE, 0, OTA_MD5_LEN / 2);

  Serial.printf("Letztes OTA-Ergebnis %u (Detail %u)\n", last, detail);
}

// CMD 50 (aus loop()-Kontext)
inline void otaStart() {
  if (_otaRunning) {
    Serial.println("⚠️ OTA läuft bereits");
    return;
  }
  _otaTakeString(OTA_SSID_LEN_REG, OTA_SSID_BASE, OTA_SSID_MAX, _otaSsid);
  _otaTakeString(OTA_PASS_LEN_REG, OTA_PASS_BASE, OTA_PASS_MAX, _otaPass);
  _otaTakeString(OTA_URL_LEN_REG, OTA_URL_BASE, OTA_URL_MAX, _otaUrl);

  for (uint16_t i = 0; i < OTA_MD5_LEN / 2; i++) {
    uint16_t w = mb.Hreg(OTA_MD5_BASE + i);
    _otaMd5[2 * i]     = (char)(w >> 8);
    _otaMd5[2 * i + 1] = (char)(w & 0xFF);
    mb.Hreg(OTA_MD5_BASE + i, 0);
  }
  _otaMd5[OTA_MD5_LEN] = '\0';
  if (strlen(_otaMd5) != OTA_MD5_LEN) _otaMd5[0] = '\0';  // unvollständig/leer = keine Prüfung

  _otaProgress = 0;
  _otaDetail = 0;
  _otaAbort = false;
  if (_otaSsid[0] == '\0' || strncmp(_otaUrl, "http", 4) != 0) {
    _otaWipeSecrets();
    _otaStatus = OTA_ST_ERR_PARAMS;
    Serial.println("❌ OTA: SSID oder URL fehlt");
    return;
  }

  Serial.printf("🔄 OTA: SSID '%s', URL %s, MD5 %s\n", _otaSsid, _otaUrl, _otaMd5[0] ? _otaMd5 : "-");
  _otaSaveResult(OTA_ST_STARTED, 0);  // bleibt stehen, falls der Knoten mittendrin neu startet
  _otaRunning = true;
  _otaStatus = OTA_ST_WIFI;
  if (xTaskCreatePinnedToCore(_otaTask, "ota", 12288, nullptr, 1, nullptr, 0) != pdPASS) {
    _otaWipeSecrets();
    _otaSaveResult(OTA_ST_ERR_BEGIN, 0);
    _otaStatus = OTA_ST_ERR_BEGIN;
    _otaRunning = false;
  }
}

// CMD 51
inline void otaAbort() {
  if (_otaRunning) {
    Serial.println("⏹️ OTA wird abgebrochen");
    _otaAbort = true;
  }
}

// Aus modbusTask() aufrufen: Task-Zustand in die Register spiegeln
inline void otaSyncRegisters() {
  mb.Hreg(OTA_STATUS_REG, _otaStatus);
  mb.Hreg(OTA_PROGRESS_REG, _otaProgress);
  mb.Hreg(OTA_DETAIL_REG, _otaDetail);
}

// Statt loraUiTask(), solange otaActive()
inline void otaUiTask(uint32_t periodMs = 500) {
  uint32_t now = millis();
  if (now - _otaLastUiMs < periodMs) return;
  _otaLastUiMs = now;
  oledDrawOta(_otaStatus, _otaProgress, SLAVE_ID);
}
