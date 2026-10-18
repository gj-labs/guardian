"""Camera checks: "is a person on the floor?"

Backends (pick with config `vision.backend`):
  vlm      InternVL3-1B (QCS8275 QNN .pte from the track Drive) kept loaded in
           qnn_multimodal_persistent. Answers yes/no and writes a one-line description.
  encoder  Only the InternVL3 vision encoder .pte, run with qnn_executor_runner.
           Frames are embedded and compared with your own reference photos
           (refs/floor, refs/upright, refs/empty). Fallback if the VLM runner is not ready.
  mock     No model: the dashboard's "person on floor" toggle decides. For rehearsal off-board.
"""
from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("guardian.vision")

FLOOR_PROMPT = ("Look at this photo of a room. Is a person lying, sitting or collapsed on the floor? "
                "Answer yes or no first, then one short sentence saying where they are.")

# InternVL preprocessing (ImageNet mean/std, 448x448 tile).
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


@dataclass
class VisionResult:
    on_floor: bool | None          # None = check failed
    description: str
    latency_ms: float
    backend: str
    confidence: float | None = None


# ---------------------------------------------------------------- helpers

def preprocess(frame_bgr: np.ndarray, size: int = 448) -> np.ndarray:
    """BGR uint8 HxWx3 -> contiguous float32 [1,3,size,size], normalised."""
    import cv2
    rgb = cv2.cvtColor(cv2.resize(frame_bgr, (size, size), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    x = (rgb.astype(np.float32) / 255.0 - MEAN) / STD
    return np.ascontiguousarray(x.transpose(2, 0, 1)[None])


def parse_yes_no(text: str) -> bool | None:
    m = re.search(r"\b(yes|no)\b", text.lower())
    return None if m is None else m.group(1) == "yes"


# ---------------------------------------------------------------- backends

class MockVision:
    name = "mock"

    def __init__(self) -> None:
        self.on_floor = False
        self.delay_s = 0.3

    def check(self, frame: np.ndarray | None) -> VisionResult:
        t0 = time.monotonic()
        time.sleep(self.delay_s)
        desc = "Person lying on the floor near the couch." if self.on_floor else "Person standing in the room."
        return VisionResult(self.on_floor, desc, (time.monotonic() - t0) * 1000, self.name, 1.0)


class VlmVision:
    """InternVL3-1B on the NPU through `qnn_multimodal_persistent` (shipped in the track's
    ExecuTorch source archive). The runner loads the three .pte files once (~8 s) and then
    answers one request per stdin line:  <image.raw>\t<prompt>\t<seq_len>  ->  RESULT_READY n ...
    The generated text is written to --output_path.
    """

    name = "vlm"

    def __init__(self, runner: str, model_dir: str, model_prefix: str, workdir: str,
                 seq_len: int = 330, timeout_s: float = 30.0, load_timeout_s: float = 90.0,
                 env: dict[str, str] | None = None) -> None:
        self.runner, self.model_dir, self.prefix = runner, Path(model_dir), model_prefix
        self.workdir = Path(workdir)
        self.seq_len, self.timeout_s, self.load_timeout_s = seq_len, timeout_s, load_timeout_s
        self.env = {**os.environ, **(env or {})}
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._req = 0

    def _argv(self) -> list[str]:
        m = self.model_dir
        return [self.runner, "--decoder_model_version", "internvl3",
                "--decoder_path", str(m / f"{self.prefix}.pte"),
                "--encoder_path", str(m / f"{self.prefix}_vision_encoder_qnn.pte"),
                "--tok_embedding_path", str(m / f"{self.prefix}_tok_embedding_qnn.pte"),
                "--tokenizer_path", str(m / "tokenizer.json"),
                "--output_path", str(self.workdir / "vlm_out.txt"),
                "--seq_len", str(self.seq_len)]

    def start(self) -> None:
        """Launch the runner and wait for READY (model + QNN contexts loaded)."""
        if self._proc is not None and self._proc.poll() is None:
            return
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._lines = queue.Queue()
        log_f = open(self.workdir / "vlm_runner.log", "ab")
        self._proc = subprocess.Popen(self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=log_f, cwd=self.workdir, env=self.env, text=True, bufsize=1)
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        line = self._wait(lambda ln: ln == "READY", self.load_timeout_s)
        if line != "READY":
            self.stop()
            raise RuntimeError(f"runner did not become ready ({line!r}); see {self.workdir}/vlm_runner.log")

    def _pump(self, proc: subprocess.Popen) -> None:
        for ln in proc.stdout:
            self._lines.put(ln.strip())
        self._lines.put("<EOF>")

    def _wait(self, done, timeout: float) -> str:
        end = time.monotonic() + timeout
        while True:
            try:
                ln = self._lines.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                return "<TIMEOUT>"
            if ln == "<EOF>" or done(ln):
                return ln

    def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            with contextlib.suppress(Exception):
                self._proc.kill()
        self._proc = None

    def ask(self, frame: np.ndarray, prompt: str = FLOOR_PROMPT) -> str:
        with self._lock:
            for attempt in (0, 1):            # one restart if the runner died
                self.start()
                raw = self.workdir / "frame.raw"
                preprocess(frame).tofile(raw)
                out = self.workdir / "vlm_out.txt"
                out.unlink(missing_ok=True)
                self._proc.stdin.write(f"{raw}\t{prompt.replace(chr(9), ' ')}\t{self.seq_len}\n")
                self._proc.stdin.flush()
                ln = self._wait(lambda s: s.startswith("RESULT_"), self.timeout_s)
                if ln.startswith("RESULT_READY"):
                    return _assistant_text(out.read_text(errors="replace"))
                if ln.startswith("RESULT_ERROR"):
                    raise RuntimeError(ln)
                self.stop()                    # EOF or timeout: restart once
            raise RuntimeError(f"runner failed: {ln}")

    def check(self, frame: np.ndarray | None) -> VisionResult:
        t0 = time.monotonic()
        if frame is None:
            return VisionResult(None, "no camera frame", 0.0, self.name)
        try:
            text = self.ask(frame)
        except Exception as e:  # never crash the alert loop; fusion treats None as "ask the wearer"
            log.exception("vlm check failed")
            return VisionResult(None, f"vlm error: {e}", (time.monotonic() - t0) * 1000, self.name)
        return VisionResult(parse_yes_no(text), text, (time.monotonic() - t0) * 1000, self.name)


def _assistant_text(out: str) -> str:
    """Generated text from the runner's output file (after the last 'assistant' marker)."""
    if "assistant" in out:
        out = out.rsplit("assistant", 1)[1]
    return out.replace("<|im_end|>", "").strip()[:240]


class EncoderVision:
    """Vision-encoder embeddings + nearest reference photos.

    `embed_fn(list_of_frames) -> np.ndarray [N, D]` is injectable for tests; by default it
    runs qnn_executor_runner on the encoder .pte.
    """

    name = "encoder"
    FLOOR_LABELS = {"floor"}

    def __init__(self, refs_dir: str, embed_fn=None, k: int = 3, **runner_kw) -> None:
        self.refs_dir = Path(refs_dir)
        self.k = k
        self.embed_fn = embed_fn or QnnEncoderRunner(**runner_kw).embed
        self.ref_vecs: np.ndarray | None = None
        self.ref_labels: list[str] = []

    def load_refs(self) -> dict[str, int]:
        import cv2
        frames, labels = [], []
        for label_dir in sorted(p for p in self.refs_dir.iterdir() if p.is_dir()):
            for img in sorted(label_dir.glob("*.jpg")) + sorted(label_dir.glob("*.png")):
                f = cv2.imread(str(img))
                if f is not None:
                    frames.append(f)
                    labels.append(label_dir.name)
        if not frames:
            raise RuntimeError(f"no reference photos under {self.refs_dir}/<label>/*.jpg")
        self.ref_vecs = _l2(self.embed_fn(frames))
        self.ref_labels = labels
        return {lab: labels.count(lab) for lab in sorted(set(labels))}

    def classify(self, vec: np.ndarray) -> tuple[str, float]:
        sims = self.ref_vecs @ _l2(vec[None])[0]
        top = np.argsort(-sims)[: self.k]
        votes: dict[str, float] = {}
        for i in top:
            votes[self.ref_labels[i]] = votes.get(self.ref_labels[i], 0.0) + float(sims[i])
        label = max(votes, key=votes.get)
        return label, float(max(sims[i] for i in top if self.ref_labels[i] == label))

    def check(self, frame: np.ndarray | None) -> VisionResult:
        t0 = time.monotonic()
        if frame is None:
            return VisionResult(None, "no camera frame", 0.0, self.name)
        try:
            if self.ref_vecs is None:
                self.load_refs()
            label, sim = self.classify(self.embed_fn([frame])[0])
        except Exception as e:  # runner failures must not crash the alert loop
            log.exception("encoder check failed")
            return VisionResult(None, f"encoder error: {e}", (time.monotonic() - t0) * 1000, self.name)
        desc = {"floor": "Person on the floor.", "upright": "Person upright.",
                "empty": "Nobody in view."}.get(label, f"Looks like '{label}'.")
        return VisionResult(label in self.FLOOR_LABELS, f"{desc} (match {sim:.2f})",
                            (time.monotonic() - t0) * 1000, self.name, sim)


class QnnEncoderRunner:
    """Embeds frames with qnn_executor_runner + the vision-encoder .pte (one process per batch)."""

    def __init__(self, runner: str, model: str, size: int = 448, hidden: int | None = None,
                 output_pattern: str = "output_{i}_0.raw", timeout_s: float = 60.0,
                 env: dict[str, str] | None = None) -> None:
        self.runner, self.model, self.size = runner, model, size
        self.hidden, self.output_pattern, self.timeout_s = hidden, output_pattern, timeout_s
        self.env = {**os.environ, **(env or {})}

    def embed(self, frames: list[np.ndarray]) -> np.ndarray:
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            lines = []
            for i, f in enumerate(frames):
                p = d / f"in_{i}.raw"
                preprocess(f, self.size).tofile(p)
                lines.append(p.name)
            (d / "input_list.txt").write_text("\n".join(lines) + "\n")
            (d / "out").mkdir()
            cmd = [self.runner, "--model_path", self.model, "--input_list_path", "input_list.txt",
                   "--output_folder_path", "out"]
            res = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=self.timeout_s,
                                 env=self.env)
            if res.returncode != 0:
                raise RuntimeError(f"qnn_executor_runner exit {res.returncode}: {res.stderr[-300:]}")
            vecs = []
            for i in range(len(frames)):
                out = d / "out" / self.output_pattern.format(i=i)
                if not out.exists():
                    raise RuntimeError(f"missing runner output {out.name}; check output_pattern")
                v = np.fromfile(out, dtype=np.float32)
                if self.hidden and v.size % self.hidden == 0:
                    v = v.reshape(-1, self.hidden).mean(axis=0)   # mean-pool image tokens
                vecs.append(v)
            return np.stack(vecs)


def _l2(x: np.ndarray) -> np.ndarray:
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-8)


def make_vision(cfg: dict):
    kind = cfg.get("backend", "mock")
    env = {"ADSP_LIBRARY_PATH": cfg.get("adsp_library_path", "/usr/lib")}
    if kind == "vlm":
        return VlmVision(cfg["vlm_runner"], cfg["model_dir"], cfg["model_prefix"],
                         cfg.get("workdir", "data/vlm"), cfg.get("seq_len", 330),
                         cfg.get("timeout_s", 30.0), cfg.get("load_timeout_s", 90.0),
                         {**env, "LD_LIBRARY_PATH": cfg.get("ld_library_path", "")})
    if kind == "encoder":
        return EncoderVision(cfg.get("refs_dir", "refs"), runner=cfg["runner"], model=cfg["encoder_model"],
                             size=cfg.get("size", 448), hidden=cfg.get("hidden"),
                             output_pattern=cfg.get("output_pattern", "output_{i}_0.raw"), env=env)
    return MockVision()
