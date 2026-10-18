"""Settings: defaults below, overridden by a TOML file (GUARDIAN_CONFIG or board/config.toml)."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

BOARD_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "dev": True,                      # enables /api/sim/* buttons on the dashboard
    "data_dir": "data",
    "bridge": "mock",                 # "router" on the VENTUNO Q, "mock" elsewhere
    "camera": {"device": "none", "fps": 5, "width": 640, "height": 480},  # device 0 for the USB cam
    "vision": {"backend": "mock", "keep_snapshots": True},
    "mic": {"device": "none", "rise_db": 22.0, "min_dbfs": -32.0},   # "plughw:1,0" = Brio 101 mic
    "fusion": {"checkin_s": 20, "verify_timeout_s": 15, "settle_s": 1.5, "recheck_s": 4, "rearm_s": 10,
               "escalate_s": 60},
    # SMS when nobody answers the watch alert. provider "log" only records it; "twilio" sends it
    # (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN env vars on the board).
    "sms": {"provider": "log", "from": "", "to": [], "place": "home"},
    "tts": {"piper_bin": "", "piper_model": "", "piper_rate": 22050, "speaker": "none"},
    "watch": {"offline_after_s": 10},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str | None = None) -> dict:
    path = path or os.environ.get("GUARDIAN_CONFIG") or str(BOARD_DIR / "config.toml")
    cfg = DEFAULTS
    if Path(path).exists():
        with open(path, "rb") as f:
            cfg = _merge(DEFAULTS, tomllib.load(f))
    data_dir = Path(cfg["data_dir"])
    cfg["data_dir"] = str(data_dir if data_dir.is_absolute() else BOARD_DIR / data_dir)
    return cfg
