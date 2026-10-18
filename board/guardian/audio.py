"""Thud detector on the USB camera's microphone (CPU, always on).

Reads 16 kHz mono from `arecord`, measures loudness in 50 ms blocks, and fires when a
block is much louder than the room's recent background (a body or object hitting the
floor). It only wakes the camera check; it never alerts on its own. No audio is stored.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading
import time
from typing import Callable

import numpy as np

log = logging.getLogger("guardian.audio")
RATE = 16000
BLOCK = RATE // 20          # 50 ms


def dbfs(block: np.ndarray) -> float:
    rms = math.sqrt(float(np.mean(block.astype(np.float32) ** 2))) if block.size else 0.0
    return 20 * math.log10(max(rms, 1.0) / 32768.0)


class ThudDetector:
    """Pure loudness logic, separate from the audio source so it can be tested."""

    def __init__(self, rise_db: float = 22.0, min_dbfs: float = -32.0, cooldown_s: float = 5.0) -> None:
        self.rise_db, self.min_dbfs, self.cooldown_s = rise_db, min_dbfs, cooldown_s
        self.floor_db: float | None = None
        self._last = -1e9

    def feed(self, now: float, level_db: float) -> bool:
        if self.floor_db is None:
            self.floor_db = level_db
            return False
        hit = (level_db >= self.min_dbfs and level_db - self.floor_db >= self.rise_db
               and now - self._last >= self.cooldown_s)
        if hit:
            self._last = now
        else:
            # Track the background slowly so a loud event does not raise it much.
            self.floor_db += 0.02 * (level_db - self.floor_db)
        return hit


class Microphone:
    def __init__(self, device: str, on_thud: Callable[[float, float], None], **detector_kw) -> None:
        self.device, self.on_thud = device, on_thud
        self.detector = ThudDetector(**detector_kw)
        self.level_db: float | None = None
        self.active = False
        self.error: str | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="mic").start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            self._proc.kill()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["arecord", "-q", "-D", self.device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except OSError as e:
                self.error, self.active = str(e), False
                return
            self.active, self.error = True, None
            while not self._stop.is_set():
                data = self._proc.stdout.read(BLOCK * 2)
                if len(data) < BLOCK * 2:
                    break
                now = time.monotonic()
                self.level_db = dbfs(np.frombuffer(data, dtype=np.int16))
                if self.detector.feed(now, self.level_db):
                    self.on_thud(now, self.level_db)
            self.active = False
            if not self._stop.is_set():
                self.error = (self._proc.stderr.read() or b"").decode(errors="replace")[-200:] or "mic stopped"
                time.sleep(2)   # device unplugged or busy: retry
