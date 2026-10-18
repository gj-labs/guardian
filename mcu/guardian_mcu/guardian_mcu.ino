// Guardian STM32H5 sketch for Arduino VENTUNO Q.
// Linux calls guardian_set(state, camera_on, watch_online, countdown_s) through Arduino Bridge
// about once a second. This sketch owns the Modulino Pixels: the alarm patterns and the countdown
// bar are timed here, and if Linux stops calling for HEARTBEAT_TIMEOUT_MS it shows a fault
// (blinking blue) so nobody mistakes a crashed app for "all quiet".
//
// Pixels (0 = left):
//   MONITORING  pixel 0 dim green; pixel 6 dim yellow if the watch is offline
//   VERIFYING   amber sweep
//   CHECKIN     amber countdown bar (pixels 0-5 shrink over the check-in time), pulsing
//   ALERT       all red, flashing 2 Hz (latched until Linux sends MONITORING)
//   FAULT       pixel 0 blinking blue (no heartbeat from Linux)
//   pixel 7     white while the camera is on (privacy light, MCU-controlled)

#include <Arduino_RouterBridge.h>
#include <Modulino.h>

// The Qwiic connector's I2C bus. On UNO Q it is Wire1; check the VENTUNO Q pinout and change if needed.
#ifndef MODULINO_WIRE
#define MODULINO_WIRE Wire1
#endif

const unsigned long HEARTBEAT_TIMEOUT_MS = 5000;
const int N = 8;

enum GuardianState { MONITORING = 0, VERIFYING = 1, CHECKIN = 2, ALERT = 3 };

ModulinoPixels pixels;

volatile int g_state = MONITORING;
volatile bool g_camera = false;
volatile bool g_watch = false;
volatile int g_countdown = 0;
volatile unsigned long g_last_call = 0;
volatile unsigned long g_checkin_start = 0;
volatile int g_checkin_total = 20;

bool guardian_set(int state, bool camera_on, bool watch_online, int countdown_s) {
  if (state == CHECKIN && g_state != CHECKIN) {
    g_checkin_start = millis();
    g_checkin_total = countdown_s > 0 ? countdown_s : 20;
  }
  g_state = state;
  g_camera = camera_on;
  g_watch = watch_online;
  g_countdown = countdown_s;
  g_last_call = millis();
  return true;
}

void setup() {
  Modulino.begin(MODULINO_WIRE);
  pixels.begin();
  pixels.clear();
  pixels.show();
  if (!Bridge.begin()) {
    while (true) {}
  }
  // provide_safe: runs in the loop context, keeps I2C access single-threaded.
  Bridge.provide_safe("guardian_set", guardian_set);
}

static uint8_t pulse(unsigned long now, unsigned long period_ms, uint8_t lo, uint8_t hi) {
  unsigned long p = now % period_ms;
  unsigned long half = period_ms / 2;
  unsigned long x = p < half ? p : period_ms - p;
  return lo + (uint8_t)((hi - lo) * x / half);
}

void render(unsigned long now) {
  pixels.clear();
  bool alive = g_last_call != 0 && now - g_last_call < HEARTBEAT_TIMEOUT_MS;

  if (!alive) {
    if ((now / 500) % 2) pixels.set(0, BLUE, 30);
    pixels.show();
    return;
  }

  switch (g_state) {
    case MONITORING:
      pixels.set(0, GREEN, 8);
      if (!g_watch) pixels.set(6, YELLOW, 10);
      break;
    case VERIFYING: {
      int pos = (now / 120) % 6;
      pixels.set(pos, 255, 120, 0, 40);
      break;
    }
    case CHECKIN: {
      // MCU-side countdown so the bar keeps moving smoothly between heartbeats.
      long left_ms = (long)g_checkin_total * 1000 - (long)(now - g_checkin_start);
      int lit = left_ms <= 0 ? 0 : 1 + (int)((left_ms * 5) / ((long)g_checkin_total * 1000));
      uint8_t b = pulse(now, 1000, 15, 60);
      for (int i = 0; i < lit && i < 6; i++) pixels.set(i, 255, 120, 0, b);
      break;
    }
    case ALERT:
      if ((now / 250) % 2) {
        for (int i = 0; i < 7; i++) pixels.set(i, RED, 80);
      }
      break;
  }
  if (g_camera) pixels.set(7, 255, 255, 255, 10);
  pixels.show();
}

void loop() {
  static unsigned long last = 0;
  unsigned long now = millis();
  if (now - last >= 33) {   // ~30 fps, non-blocking so Bridge safe callbacks keep running
    last = now;
    render(now);
  }
}
