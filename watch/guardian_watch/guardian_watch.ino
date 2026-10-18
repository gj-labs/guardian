// Guardian family alert for M5Stack StopWatch (ESP32-S3), worn by a family member or caregiver.
// - Connects to the Guardian board over Wi-Fi (WebSocket /ws/watch).
// - "checking": amber screen while the board's camera looks at a possible fall.
// - "alert": flashing red, vibration and a beep, with the camera model's description.
//   Tap the screen or the blue key to answer "I'm on my way" (the board tells the person).
// - "clear": green confirmation (false alarm, person got up, or someone is on the way).
//
// Libraries (Arduino Library Manager): M5Unified, WebSockets (Markus Sattler), ArduinoJson 7.
// Board: "M5StopWatch" from the M5Stack board package.
// Copy config.example.h to config.h and fill in Wi-Fi and the VENTUNO Q address.

#include <M5Unified.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include "config.h"

WebSocketsClient ws;
bool wsConnected = false;

enum Screen { S_CONNECTING, S_HOME, S_CHECKING, S_ALERT, S_ACKED, S_CLEAR };
Screen screen = S_CONNECTING;
uint32_t screenSince = 0;
String screenText;
bool redraw = true;

// ---------------------------------------------------------------- helpers
void sendType(const char* type) {
  if (!wsConnected) return;
  JsonDocument d;
  d["type"] = type;
  String s;
  serializeJson(d, s);
  ws.sendTXT(s);
}

void setScreen(Screen s, const String& text = "") {
  screen = s;
  screenSince = millis();
  screenText = text;
  redraw = true;
  if (s != S_ALERT) M5.Power.setVibration(0);
}

// ---------------------------------------------------------------- websocket
void onWs(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      setScreen(S_HOME);
      break;
    case WStype_DISCONNECTED:
      wsConnected = false;
      if (screen != S_ALERT) setScreen(S_CONNECTING);   // keep showing an alert even if Wi-Fi drops
      break;
    case WStype_TEXT: {
      JsonDocument d;
      if (deserializeJson(d, payload, length)) return;
      const char* t = d["type"] | "";
      const char* text = d["text"] | "";
      if (!strcmp(t, "checking")) setScreen(S_CHECKING, text);
      else if (!strcmp(t, "alert")) setScreen(S_ALERT, text);
      else if (!strcmp(t, "clear")) { if (screen != S_HOME) setScreen(S_CLEAR, text); }
      else if (!strcmp(t, "state")) {
        const char* st = d["state"] | "MONITORING";
        if (!strcmp(st, "ALERT")) setScreen(S_ALERT, text);
        else if (!strcmp(st, "VERIFYING") || !strcmp(st, "CHECKIN")) setScreen(S_CHECKING, text);
        else setScreen(S_HOME);
      }
      break;
    }
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

// Word-wrap `s` into lines of at most `maxW` pixels, centred, starting at y.
void drawWrapped(const String& s, int y, int maxW, const lgfx::IFont* font, uint16_t color, int maxLines = 3) {
  M5.Display.setFont(font);
  int lineH = M5.Display.fontHeight() + 4;
  String line, word;
  int lines = 0;
  for (int i = 0; i <= (int)s.length() && lines < maxLines; i++) {
    char c = i < (int)s.length() ? s[i] : ' ';
    if (c != ' ') { word += c; continue; }
    String trial = line.length() ? line + " " + word : word;
    if (M5.Display.textWidth(trial) > maxW && line.length()) {
      drawCentered(line, y + lines * lineH, font, color);
      lines++;
      line = word;
    } else {
      line = trial;
    }
    word = "";
  }
  if (line.length() && lines < maxLines) drawCentered(line, y + lines * lineH, font, color);
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
      drawCentered("All quiet at home", cy + 10, &fonts::FreeSans12pt7b, TFT_WHITE);
      break;
    case S_CHECKING:
      d.fillScreen(0xFC60);  // amber
      drawCentered("Possible fall", cy - 40, &fonts::FreeSansBold18pt7b, TFT_BLACK);
      drawCentered("checking...", cy + 10, &fonts::FreeSans12pt7b, TFT_BLACK);
      break;
    case S_ALERT:
      d.fillScreen((el / 500) % 2 ? TFT_RED : 0x8000);
      drawCentered("FALL", cy - 110, &fonts::FreeSansBold24pt7b, TFT_WHITE);
      drawWrapped(screenText.length() ? screenText : String("Still on the floor"), cy - 45, d.width() - 110,
                  &fonts::FreeSans12pt7b, TFT_WHITE);
      drawCentered("Tap: I'm on my way", cy + 110, &fonts::FreeSansBold12pt7b, TFT_WHITE);
      break;
    case S_ACKED:
      d.fillScreen(TFT_DARKGREEN);
      drawCentered("Sent:", cy - 25, &fonts::FreeSans12pt7b, TFT_WHITE);
      drawCentered("on my way", cy + 20, &fonts::FreeSansBold18pt7b, TFT_WHITE);
      break;
    case S_CLEAR:
      d.fillScreen(TFT_DARKGREEN);
      drawCentered("OK", cy - 40, &fonts::FreeSansBold24pt7b, TFT_WHITE);
      drawWrapped(screenText, cy + 10, d.width() - 110, &fonts::FreeSans12pt7b, TFT_WHITE);
      break;
  }
}

// ---------------------------------------------------------------- main
void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setBrightness(160);
  M5.Speaker.begin();
  M5.Speaker.setVolume(200);
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

  static uint32_t lastHb = 0;
  if (wsConnected && now - lastHb > 2000) {
    lastHb = now;
    JsonDocument d;
    d["type"] = "hb";
    d["battery"] = M5.Power.getBatteryLevel();
    String s;
    serializeJson(d, s);
    ws.sendTXT(s);
  }

  // Tap or blue key during a possible fall or alert = "I'm on my way".
  bool tapped = M5.Touch.getDetail().wasClicked() || M5.BtnB.wasClicked() || M5.BtnA.wasClicked();
  if ((screen == S_ALERT || screen == S_CHECKING) && tapped) {
    sendType("ack");
    setScreen(S_ACKED);
  }

  // Alert: vibrate and beep in bursts until acknowledged.
  uint32_t el = now - screenSince;
  if (screen == S_ALERT) {
    bool on = (el % 1000) < 300;
    M5.Power.setVibration(on ? 255 : 0);
    static bool beeped = false;
    if (on && !beeped) { M5.Speaker.tone(2200, 200); beeped = true; }
    if (!on) beeped = false;
  }
  if ((screen == S_CLEAR || screen == S_ACKED) && el > 4000) setScreen(wsConnected ? S_HOME : S_CONNECTING);

  static uint32_t lastDraw = 0;
  if (redraw || (screen == S_ALERT && now - lastDraw > 250)) {
    redraw = false;
    lastDraw = now;
    drawScreen();
  }
}
