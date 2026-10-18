#!/usr/bin/env bash
# Run on the VENTUNO Q: install and start the Guardian MCU App (compiles + flashes the STM32).
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/guardian-mcu"
dst="$HOME/ArduinoApps/guardian-mcu"
mkdir -p "$dst"
cp -r "$src/." "$dst/"
arduino-app-cli app restart -v "$dst"
