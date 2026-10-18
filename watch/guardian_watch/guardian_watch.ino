// Guardian wrist unit for M5Stack StopWatch (ESP32-S3).
// - Detects a fall-like impact followed by stillness with the BMI270 and tells the board.
// - Shows the "Are you OK?" check-in, vibrates, plays the board's speech, takes the "I'm OK" tap.
// - Hold the yellow key (BtnA) 2 s for SOS.
//
// Libraries (Arduino Library Manager): M5Unified, WebSockets (Markus Sattler), ArduinoJson 7.
// Board: "M5StopWatch" from the M5Stack board package (or ESP32S3 Dev Module with PSRAM: OPI).
// Copy config.example.h to config.h and fill in Wi-Fi and the VENTUNO Q address.

#include <M5Unified.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "config.h"

// ---- fall detection tuning (g units, wrist-worn) ----
const float IMPACT_G = 2.5f;         // peak acceleration that counts as an impact
const float STILL_DEV_G = 0.12f;     // max deviation from the mean while lying still
const uint32_t STILL_MS = 1500;      // stillness required after the impact
const uint32_t IMPACT_WINDOW_MS = 400;
const uint32_t COOLDOWN_MS = 8000;

WebSocketsClient ws;
bool wsConnected = false;

enum Screen { S_CONNECTING, S_HOME, S_CHECKIN, S_ALERT, S_CLEAR };
Screen screen = S_CONNECTING;
uint32_t screenSince = 0;
float checkinSeconds = 20;
String screenText;
bool redraw = true;

// audio: the board sends {"type":"audio","bytes":N} then one binary frame of 16 kHz int16 mono
int16_t* audioBuf = nullptr;
size_t expectAudio = 0;

// ---------------------------------------------------------------- helpers
void sendJson(JsonDocument& doc) {
  if (!wsConnected) return;
  String s;
  serializeJson(doc, s);
  ws.sendTXT(s);
}

void sendType(const char* type) {
  JsonDocument d;
  d["type"] = type;
  sendJson(d);
}

void setScreen(Screen s, const String& text = "") {
  screen = s;
  screenSince = millis();
  screenText = text;
  redraw = true;
  if (s != S_CHECKIN && s != S_ALERT) M5.Power.setVibration(0);
}

void playAudio(const uint8_t* data, size_t len) {
  M5.Speaker.stop();
  if (audioBuf) free(audioBuf);
  audioBuf = (int16_t*)ps_malloc(len);
  if (!audioBuf) return;
  memcpy(audioBuf, data, len);
  M5.Speaker.playRaw(audioBuf, len / 2, 16000, false, 1, 0, true);
}

// ---------------------------------------------------------------- websocket
void onWs(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      sendType("hello");
      setScreen(S_HOME);
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      if (screen != S_CONNECTING) setScreen(S_CONNECTING);
      break;
    case WStype_TEXT: {
      JsonDocument d;
      if (deserializeJson(d, payload, length)) return;
      const char* t = d["type"] | "";
      if (!strcmp(t, "checkin")) {
        checkinSeconds = d["seconds"] | 20.0f;
        setScreen(S_CHECKIN, d["text"] | "Are you OK?");
      } else if (!strcmp(t, "alert")) {
        setScreen(S_ALERT, d["text"] | "Help is on the way");
      } else if (!strcmp(t, "clear")) {
        setScreen(S_CLEAR, d["text"] | "Glad you are OK");
      } else if (!strcmp(t, "audio")) {
        expectAudio = d["bytes"] | 0;
      } else if (!strcmp(t, "state")) {
        const char* st = d["state"] | "MONITORING";
        if (!strcmp(st, "CHECKIN")) { checkinSeconds = d["seconds"] | 20.0f; setScreen(S_CHECKIN, "Are you OK?"); }
        else if (!strcmp(st, "ALERT")) setScreen(S_ALERT, "Help is on the way");
        else setScreen(S_HOME);
      }
      break;
    }
    case WStype_BIN:
      if (expectAudio && length == expectAudio) playAudio(payload, length);
      expectAudio = 0;
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------- UI
void drawCentered(const String& s, int y, const lgfx::IFont* font, uint16_t color) {
  M5.Display.setFont(font);
  M5.Display.setTextColor(color);
  M5.Display.setTextDatum(middle_center);
  M5.Display.drawString(s, M5.Display.width() / 2, y);
}

void drawScreen() {
  auto& d = M5.Display;
  int cx = d.width() / 2, cy = d.height() / 2;
  uint32_t el = millis() - screenSince;
  switch (screen) {
    case S_CONNECTING:
      d.fillScreen(TFT_BLACK);
      drawCentered("Guardian", cy - 30, &fonts::FreeSansBold18pt7b, TFT_WHITE);
      drawCentered("connecting...", cy + 25, &fonts::FreeSans12pt7b, TFT_DARKGREY);
      break;
    case S_HOME:
      d.fillScreen(TFT_BLACK);
      d.drawCircle(cx, cy, cx - 6, TFT_DARKGREEN);
      d.drawCircle(cx, cy, cx - 7, TFT_DARKGREEN);
      drawCentered("Guardian", cy - 40, &fonts::FreeSansBold18pt7b, TFT_GREEN);
      drawCentered("I'm watching out", cy + 10, &fonts::FreeSans12pt7b, TFT_WHITE);
      drawCentered("Hold yellow 2s for SOS", cy + 60, &fonts::FreeSans9pt7b, TFT_DARKGREY);
      break;
    case S_CHECKIN: {
      int left = max(0, (int)ceilf(checkinSeconds - el / 1000.0f));
      d.fillScreen(0xFC60);  // amber
      float frac = checkinSeconds > 0 ? left / checkinSeconds : 0;
      d.fillArc(cx, cy, cx - 4, cx - 22, -90, -90 + (int)(360 * frac), TFT_WHITE);
      drawCentered("Are you OK?", cy - 50, &fonts::FreeSansBold18pt7b, TFT_BLACK);
      drawCentered(String(left), cy + 15, &fonts::FreeSansBold24pt7b, TFT_BLACK);
      drawCentered("Tap if you're fine", cy + 75, &fonts::FreeSans12pt7b, TFT_BLACK);
      break;
    }
    case S_ALERT:
      d.fillScreen((el / 500) % 2 ? TFT_RED : 0x8000);
      drawCentered("Help is", cy - 25, &fonts::FreeSansBold24pt7b, TFT_WHITE);
      drawCentered("on the way", cy + 25, &fonts::FreeSansBold24pt7b, TFT_WHITE);
      drawCentered("Tap if you're OK", cy + 85, &fonts::FreeSans12pt7b, TFT_WHITE);
      break;
    case S_CLEAR:
      d.fillScreen(TFT_DARKGREEN);
      drawCentered("Glad you're OK", cy, &fonts::FreeSansBold18pt7b, TFT_WHITE);
      break;
  }
}

// ---------------------------------------------------------------- fall detection
enum FallPhase { F_IDLE, F_IMPACT, F_STILL };
FallPhase fall = F_IDLE;
uint32_t fallT = 0, lastFall = 0;
float peakG = 0, sum = 0, sumSq = 0;
int nStill = 0;

void sendImpact(float peak) {
  JsonDocument d;
  d["type"] = "impact";
  d["peak_g"] = peak;
  sendJson(d);
}

void updateFall(uint32_t now) {
  float ax, ay, az;
  if (!M5.Imu.getAccel(&ax, &ay, &az)) return;
  float g = sqrtf(ax * ax + ay * ay + az * az);
  switch (fall) {
    case F_IDLE:
      if (g > IMPACT_G && now - lastFall > COOLDOWN_MS && screen == S_HOME) {
        fall = F_IMPACT; fallT = now; peakG = g;
      }
      break;
    case F_IMPACT:
      peakG = max(peakG, g);
      if (now - fallT > IMPACT_WINDOW_MS) { fall = F_STILL; fallT = now; sum = sumSq = 0; nStill = 0; }
      break;
    case F_STILL: {
      sum += g; sumSq += g * g; nStill++;
      float mean = sum / nStill;
      float dev = sqrtf(max(0.0f, sumSq / nStill - mean * mean));
      if (nStill > 20 && dev > STILL_DEV_G) { fall = F_IDLE; break; }   // moving again: not a fall
      if (now - fallT > STILL_MS) { fall = F_IDLE; lastFall = now; sendImpact(peakG); }
      break;
    }
  }
}

// ---------------------------------------------------------------- main
void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setBrightness(160);
  M5.Speaker.begin();
  M5.Speaker.setVolume(220);
  drawScreen();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  ws.begin(BOARD_HOST, BOARD_PORT, "/ws/watch");
  ws.onEvent(onWs);
  ws.setReconnectInterval(2000);
  ws.enableHeartbeat(5000, 3000, 2);
}

void loop() {
  M5.update();
  ws.loop();
  uint32_t now = millis();

  static uint32_t lastImu = 0;
  if (now - lastImu >= 10) { lastImu = now; updateFall(now); }   // 100 Hz

  static uint32_t lastHb = 0;
  if (wsConnected && now - lastHb > 2000) {
    lastHb = now;
    JsonDocument d;
    d["type"] = "hb";
    d["battery"] = M5.Power.getBatteryLevel();
    sendJson(d);
  }

  // input: tap or blue key = I'm OK; hold yellow key = SOS
  auto t = M5.Touch.getDetail();
  bool tapped = t.wasClicked() || M5.BtnB.wasClicked();
  if ((screen == S_CHECKIN || screen == S_ALERT) && tapped) {
    sendType("ok");
    setScreen(S_CLEAR, "Glad you are OK");
  }
  if (M5.BtnA.wasHold() && screen != S_ALERT) {
    sendType("sos");
    setScreen(S_ALERT, "Help is on the way");
  }

  // vibration patterns
  uint32_t el = now - screenSince;
  if (screen == S_CHECKIN) M5.Power.setVibration((el % 1000) < 300 ? 200 : 0);
  else if (screen == S_ALERT) M5.Power.setVibration((el % 600) < 200 ? 255 : 0);
  if (screen == S_CLEAR && el > 3000) setScreen(wsConnected ? S_HOME : S_CONNECTING);

  // redraw animated screens a few times a second, static ones only on change
  static uint32_t lastDraw = 0;
  bool animated = screen == S_CHECKIN || screen == S_ALERT;
  if (redraw || (animated && now - lastDraw > 250)) {
    redraw = false;
    lastDraw = now;
    drawScreen();
  }
}
