"""Text to speech on the board, streamed to the StopWatch as 16 kHz mono int16 PCM.

Uses Piper (https://github.com/rhasspy/piper) if a voice model is configured, else
espeak-ng, else no audio (the watch still shows the text). Phrases are cached, so the
fixed check-in/alert lines cost nothing at incident time.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import wave

import numpy as np

log = logging.getLogger("guardian.tts")
OUT_RATE = 16000


def _resample(pcm: np.ndarray, rate: int) -> np.ndarray:
    if rate == OUT_RATE or pcm.size == 0:
        return pcm
    n = int(pcm.size * OUT_RATE / rate)
    x = np.interp(np.linspace(0, pcm.size - 1, n), np.arange(pcm.size), pcm.astype(np.float32))
    return x.astype(np.int16)


class Speech:
    def __init__(self, piper_bin: str | None = None, piper_model: str | None = None,
                 piper_rate: int = 22050) -> None:
        self.piper_bin = piper_bin or shutil.which("piper")
        self.piper_model, self.piper_rate = piper_model, piper_rate
        self.espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        self._cache: dict[str, bytes] = {}

    @property
    def engine(self) -> str:
        if self.piper_bin and self.piper_model:
            return "piper"
        return "espeak" if self.espeak else "none"

    def pcm16k(self, text: str) -> bytes:
        if text in self._cache:
            return self._cache[text]
        try:
            pcm = self._synth(text)
        except (OSError, subprocess.SubprocessError, wave.Error) as e:
            log.warning("tts failed (%s): %s", self.engine, e)
            pcm = b""
        self._cache[text] = pcm
        return pcm

    def _synth(self, text: str) -> bytes:
        if self.engine == "piper":
            out = subprocess.run([self.piper_bin, "--model", self.piper_model, "--output_raw"],
                                 input=text.encode(), capture_output=True, timeout=30, check=True).stdout
            return _resample(np.frombuffer(out, dtype=np.int16), self.piper_rate).tobytes()
        if self.engine == "espeak":
            out = subprocess.run([self.espeak, "--stdout", "-s", "150", text],
                                 capture_output=True, timeout=30, check=True).stdout
            with wave.open(io.BytesIO(out)) as w:
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
                if w.getnchannels() == 2:
                    pcm = pcm[::2]
                return _resample(pcm, w.getframerate()).tobytes()
        return b""
