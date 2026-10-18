"""Camera checks: "is a person on the floor?"

Backends (pick with config `vision.backend`):
  vlm      InternVL3-1B (QCS8275 QNN .pte from the track Drive) through a multimodal
           runner command. Answers yes/no and writes a one-line description.
  encoder  Only the InternVL3 vision encoder .pte, run with qnn_executor_runner.
           Frames are embedded and compared with your own reference photos
           (refs/floor, refs/upright, refs/empty). Fallback if the VLM runner is not ready.
  mock     No model: the dashboard's "person on floor" toggle decides. For rehearsal off-board.
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import tempfile
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


def _clean_answer(text: str, after: str | None) -> str:
    if after and after in text:
        text = text.split(after, 1)[1]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Runners print logs around the generation; keep the lines that read like an answer.
    keep = [ln for ln in lines if re.search(r"\b(yes|no)\b", ln.lower())] or lines[-1:]
    return " ".join(keep)[:240]


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
    """Runs a multimodal runner per check. `command` is a template with {image} and {prompt}."""

    name = "vlm"

    def __init__(self, command: str, answer_after: str | None = None, timeout_s: float = 30.0,
                 workdir: str | None = None, env: dict[str, str] | None = None) -> None:
        self.command, self.answer_after = command, answer_after
        self.timeout_s, self.workdir = timeout_s, workdir
        self.env = {**os.environ, **(env or {})}

    def check(self, frame: np.ndarray | None) -> VisionResult:
        import cv2
        t0 = time.monotonic()
        if frame is None:
            return VisionResult(None, "no camera frame", 0.0, self.name)
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "frame.jpg"
            cv2.imwrite(str(img), frame)
            argv = [a.format(image=str(img), prompt=FLOOR_PROMPT) for a in shlex.split(self.command)]
            try:
                out = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout_s,
                                     cwd=self.workdir, env=self.env)
            except (OSError, subprocess.TimeoutExpired) as e:
                return VisionResult(None, f"runner error: {e}", (time.monotonic() - t0) * 1000, self.name)
        text = _clean_answer(out.stdout, self.answer_after)
        verdict = parse_yes_no(text)
        if out.returncode != 0 and verdict is None:
            text = f"runner exit {out.returncode}: {out.stderr.strip()[-200:]}"
        return VisionResult(verdict, text, (time.monotonic() - t0) * 1000, self.name)


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
        return VlmVision(cfg["vlm_command"], cfg.get("answer_after"), cfg.get("timeout_s", 30.0),
                         cfg.get("workdir"), env)
    if kind == "encoder":
        return EncoderVision(cfg.get("refs_dir", "refs"), runner=cfg["runner"], model=cfg["encoder_model"],
                             size=cfg.get("size", 448), hidden=cfg.get("hidden"),
                             output_pattern=cfg.get("output_pattern", "output_{i}_0.raw"), env=env)
    return MockVision()
