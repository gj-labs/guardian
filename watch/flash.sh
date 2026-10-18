#!/usr/bin/env bash
# Compile and flash the family StopWatch from a Mac/Linux machine with arduino-cli.
#   watch/flash.sh                 # auto-detects the StopWatch's USB port
#   watch/flash.sh /dev/cu.usbmodem1101
# One-time setup: arduino-cli core install m5stack:esp32 (board index:
# https://static-cdn.m5stack.com/resource/arduino/package_m5stack_index.json) and
# arduino-cli lib install M5Unified WebSockets ArduinoJson
set -euo pipefail
dir="$(cd "$(dirname "$0")" && pwd)/guardian_watch"
[ -f "$dir/config.h" ] || { echo "create $dir/config.h from config.example.h first"; exit 1; }
grep -q "TYPE-WIFI-PASSWORD-HERE" "$dir/config.h" && { echo "put your Wi-Fi password in $dir/config.h"; exit 1; }
port="${1:-$(ls /dev/cu.usbmodem* /dev/ttyACM* 2>/dev/null | grep -v -i ventuno | head -1)}"
fqbn=m5stack:esp32:m5stack_stopwatch
# tools.ctags.path: lets the build run on Apple Silicon without Rosetta (the sketch has no .ino code).
ctags_dir="$(dirname "$(command -v ctags 2>/dev/null || echo /usr/bin/ctags)")"
arduino-cli compile --fqbn "$fqbn" --build-property "tools.ctags.path=$ctags_dir" "$dir"
arduino-cli upload --fqbn "$fqbn" -p "$port" "$dir"
