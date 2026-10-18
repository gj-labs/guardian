"""Accuracy and latency of the configured vision backend on labelled photos.

    python tools/bench_vision.py test_photos/      # test_photos/<floor|upright|empty>/*.jpg

Use photos that are NOT in refs/ when benchmarking the encoder backend.
Prints per-photo verdicts, accuracy and p50/p95 latency for the README.
"""
import statistics
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from guardian import config  # noqa: E402
from guardian.vision import make_vision  # noqa: E402

root = Path(sys.argv[1])
vision = make_vision(config.load()["vision"])
rows, lat = [], []
for label_dir in sorted(p for p in root.iterdir() if p.is_dir()):
    for img in sorted(label_dir.glob("*.jpg")):
        r = vision.check(cv2.imread(str(img)))
        truth = label_dir.name == "floor"
        rows.append((img.name, label_dir.name, r.on_floor, r.on_floor == truth))
        lat.append(r.latency_ms)
        print(f"{label_dir.name:8s} {img.name:20s} on_floor={r.on_floor!s:5s} {r.latency_ms:7.0f} ms  {r.description}")

if rows:
    acc = sum(ok for *_, ok in rows) / len(rows)
    missed = sum(1 for _, lab, got, _ in rows if lab == "floor" and got is not True)
    false_alarm = sum(1 for _, lab, got, _ in rows if lab != "floor" and got is True)
    lat.sort()
    print(f"\nbackend={vision.name} n={len(rows)} accuracy={acc:.0%} missed_falls={missed} false_alarms={false_alarm}")
    print(f"latency p50={statistics.median(lat):.0f} ms p95={lat[int(0.95 * (len(lat) - 1))]:.0f} ms")
