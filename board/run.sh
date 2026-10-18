#!/usr/bin/env bash
# Start the Guardian board app. First run: creates .venv and installs requirements.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/uvicorn ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
export ADSP_LIBRARY_PATH="${ADSP_LIBRARY_PATH:-/usr/lib}"
# Reach the desktop session's PipeWire (Bluetooth / USB speakers) even when started over ssh/adb.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
exec .venv/bin/uvicorn guardian.app:app --host 0.0.0.0 --port "${PORT:-8000}"
