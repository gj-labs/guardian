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
    "vision": {"backend": "mock", "background_interval_s": 30, "keep_snapshots": True},
    "fusion": {"checkin_s": 20, "verify_timeout_s": 45, "settle_s": 1.5,
               "floor_confirmations": 2, "rearm_s": 10},
    "tts": {"piper_bin": "", "piper_model": "", "piper_rate": 22050},
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
