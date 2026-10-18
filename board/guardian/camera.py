"""USB camera capture with a short frame history and a cheap motion score.

The motion score (CPU frame differencing) gates the NPU: background checks only
run after a burst of motion followed by stillness, which is what a fall looks like.
"""
from __future__ import annotations

import collections
import threading
import time

import numpy as np


class Camera:
    def __init__(self, device: int | str = 0, fps: float = 5.0, history_s: float = 10.0,
                 width: int = 640, height: int = 480) -> None:
        self.device, self.fps = device, fps
        self.width, self.height = width, height
        self.frames: collections.deque[tuple[float, np.ndarray]] = collections.deque(
            maxlen=max(2, int(fps * history_s)))
        self.motion: collections.deque[tuple[float, float]] = collections.deque(maxlen=int(fps * 30))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.active = False
        self.error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            self.frames.clear()
        self.active = False

    def _open(self):
        import cv2
        cap = cv2.VideoCapture(self.device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        return cap

    def _run(self) -> None:
        import cv2
        cap = self._open()
        if not cap.isOpened():
            self.error, self.active = f"cannot open camera {self.device!r}", False
            return
        self.error, self.active = None, True
        prev = None
        period = 1.0 / self.fps
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                ok, frame = cap.read()
                if not ok:
                    self.error = "camera read failed"
                    time.sleep(0.5)
                    continue
                self.push(t0, frame, prev)
                prev = cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)
                time.sleep(max(0.0, period - (time.monotonic() - t0)))
        finally:
            cap.release()
            self.active = False

    def push(self, t: float, frame: np.ndarray, prev_small: np.ndarray | None = None) -> None:
        """Add a frame (also used by tests and the mock camera)."""
        score = 0.0
        if prev_small is not None:
            small = _gray_small(frame)
            score = float(np.mean(np.abs(small.astype(np.int16) - prev_small.astype(np.int16)))) / 255.0
        with self._lock:
            self.frames.append((t, frame))
            self.motion.append((t, score))

    # ------------------------------------------------------------ queries
    def latest(self) -> tuple[float, np.ndarray] | None:
        with self._lock:
            return self.frames[-1] if self.frames else None

    def frame_after(self, t: float) -> tuple[float, np.ndarray] | None:
        """First frame captured at or after time t (falls back to the newest)."""
        with self._lock:
            for ft, f in self.frames:
                if ft >= t:
                    return ft, f
            return self.frames[-1] if self.frames else None

    def burst_then_still(self, now: float, burst: float = 0.08, still: float = 0.02,
                         still_s: float = 2.0, window_s: float = 10.0) -> bool:
        """True if there was big motion in the last window and the scene has been still since."""
        with self._lock:
            pts = [(t, m) for t, m in self.motion if now - t <= window_s]
        if not pts:
            return False
        recent = [m for t, m in pts if now - t <= still_s]
        older = [m for t, m in pts if now - t > still_s]
        return bool(recent) and max(recent) < still and bool(older) and max(older) > burst


def _gray_small(frame: np.ndarray) -> np.ndarray:
    try:
        import cv2
        return cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)
    except ImportError:  # pragma: no cover - numpy-only fallback
        h, w = frame.shape[:2]
        return frame[:: max(1, h // 120), :: max(1, w // 160)].mean(axis=2).astype(np.uint8)


def jpeg(frame: np.ndarray, width: int = 480) -> bytes:
    import cv2
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, int(h * width / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else b""
