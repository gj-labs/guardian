// Copy to config.h (git-ignored) and fill in. The watch joins whichever network is in range.
#pragma once
#define WIFI_SSID "lab-wifi"
#define WIFI_PASS "lab-password"
// Optional extra networks (e.g. the demo venue):
// #define WIFI2_SSID "venue-wifi"
// #define WIFI2_PASS "venue-password"

// The board is found by mDNS name (its hostname, lowercase, without ".local").
#define BOARD_MDNS_NAME "qcventuno001"
#define BOARD_HOST "172.16.0.112"   // fallback if mDNS fails: `hostname -I` on the board
#define BOARD_PORT 8000
