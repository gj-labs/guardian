"""Capture reference photos for the encoder backend from the USB camera.

    python tools/capture_refs.py floor 15      # someone lies on the floor in different spots
    python tools/capture_refs.py upright 15    # standing, walking, sitting on the couch
    python tools/capture_refs.py empty 5       # nobody in the room

Saves board/refs/<label>/<n>.jpg, one frame per second.
"""
import sys
import time
from pathlib import Path

import cv2

label = sys.argv[1]
count = int(sys.argv[2]) if len(sys.argv) > 2 else 10
device = int(sys.argv[3]) if len(sys.argv) > 3 else 0
out = Path(__file__).resolve().parent.parent / "refs" / label
out.mkdir(parents=True, exist_ok=True)
start = len(list(out.glob("*.jpg")))

cap = cv2.VideoCapture(device)
if not cap.isOpened():
    sys.exit(f"cannot open camera {device}")
print(f"capturing {count} '{label}' frames in 3 s…")
time.sleep(3)
for i in range(count):
    ok, frame = cap.read()
    if ok:
        p = out / f"{start + i:03d}.jpg"
        cv2.imwrite(str(p), frame)
        print(p)
    time.sleep(1)
cap.release()
