# Guardian: a private fall alert on Arduino VENTUNO Q

ExecuTorch Hackathon, IoT track. Guardian detects falls at home, asks the person if they are OK, and
alerts a caregiver only when they don't answer. Video never leaves the house.

- **Wrist (M5Stack StopWatch):** spots a hard impact followed by stillness, vibrates and asks "Are you OK?", and plays speech
  generated on the board. Tap for OK, or hold the yellow key for SOS.
- **Camera + NPU (VENTUNO Q, Linux):** InternVL3-1B (ExecuTorch, QNN on the Hexagon NPU) checks the frame after the
  impact: is a person on the floor? A dropped watch with the person standing is rejected. A person found on the floor
  without a watch impact also triggers a check-in, after two background checks in a row.
- **STM32H5 + Modulino Pixels:** the board's microcontroller owns the alarm lights and the countdown bar. A camera-on
  privacy light is controlled by the MCU. If the Linux app stops sending heartbeats, the MCU shows a fault instead of "all quiet".
- **Caregiver page:** a live status page and a hash-chained care log, served by the board on the home Wi-Fi.

```text
StopWatch ──Wi-Fi WebSocket──▶ FastAPI on VENTUNO Q ──▶ ExecuTorch runner ──QNN──▶ Hexagon NPU
  (impact, OK, SOS)            fusion state machine          (InternVL3-1B / vision encoder)
     ▲  check-in, speech       │  care log (SQLite, sha256 chain)
     └─────────────────────────┤  Piper TTS (CPU)
                               └─Arduino Bridge (msgpack-RPC)─▶ STM32H5 ──I2C──▶ Modulino Pixels
Caregiver laptop ◀── http://<board>:8000 (status, log, acknowledge, pause camera)
```

States: `MONITORING → VERIFYING (camera looks) → CHECKIN (20 s to tap OK) → ALERT (caregiver)`.
A camera "upright" result returns to `MONITORING` and ignores impacts for 10 s.

| Pixels | Meaning |
|---|---|
| pixel 0 dim green | monitoring; pixel 6 yellow = watch offline |
| amber sweep | checking the camera |
| amber bar shrinking | waiting for the wearer to tap OK |
| all red flashing | caregiver alerted (latched until resolved) |
| pixel 0 blinking blue | Linux app not responding (MCU heartbeat timeout) |
| pixel 7 white | camera is on |

## Repository

| Path | What |
|---|---|
| `board/guardian/` | Linux app: `fusion.py` (state machine), `vision.py` (NPU backends), `bridge.py` (router RPC), `camera.py`, `tts.py`, `store.py`, `app.py` (FastAPI), `static/index.html` (caregiver page) |
| `board/tools/` | `fake_watch.py`, `capture_refs.py`, `bench_vision.py` |
| `board/tests/` | state machine, bridge protocol, care log, encoder matching, full app flow (`pytest`) |
| `mcu/guardian_mcu/` | STM32H5 sketch (Arduino App Lab), provides `guardian_set` over Bridge |
| `watch/guardian_watch/` | StopWatch firmware (Arduino IDE, M5Unified) |

## Models (from the track's VENTUNO Q bundle, QCS8275, QNN)

| Use | File |
|---|---|
| Fall check (primary) | `InternVL3-1B_QCS8275_ctxt_1000/` decoder, `_vision_encoder_qnn.pte`, `_tok_embedding_qnn.pte`, tokenizer |
| Fall check (fallback) | the vision encoder `.pte` alone, with `qnn_executor_runner` and your reference photos |
| Runtime | built on the board from `source/ventuno_executorch_source.tar.gz` (see `Ventuno_Q_ExecuTorch_Bringup.pdf`) |

Speech: [Piper](https://github.com/rhasspy/piper) on the CPU (not ExecuTorch), or `espeak-ng` as a fallback.

## Setup

### 1. Board (VENTUNO Q, Ubuntu)
```bash
git clone <this repo> ~/guardian-iot && cd ~/guardian-iot/board
cp config.example.toml config.toml        # edit paths, see below
sudo apt install -y python3-venv espeak-ng
./run.sh                                  # http://<board-ip>:8000
```
Then in `config.toml`:
1. Build the runner(s) from the track source archive and point `vlm_command` / `runner` at them. Run each with
   `--help` and match the flags; the `vlm_command` in the example is a template, not a verified command line.
2. Check the NPU path: `ADSP_LIBRARY_PATH`, then `bench_vision.py` on a few photos.
3. If the multimodal runner is not working, use `backend = "encoder"` and capture references:
   `python tools/capture_refs.py floor 15`, `... upright 15`, `... empty 5`.

### 2. STM32 + Pixels
Open `mcu/guardian_mcu/guardian_mcu.ino` in Arduino App Lab, add the `Arduino_Modulino` library, and plug the Pixels into the
Qwiic port. If the Pixels stay dark, change `MODULINO_WIRE` (Wire / Wire1) to the bus that the Qwiic port uses on the VENTUNO Q.
Set `bridge = "router"` in `config.toml`. Without the app running you should see pixel 0 blink blue (heartbeat timeout).

### 3. StopWatch
Arduino IDE → install the M5Stack board package and the libraries `M5Unified`, `WebSockets` (Markus Sattler) and
`ArduinoJson`. Copy `config.example.h` to `config.h` with your Wi-Fi and the board's IP, then flash.

### Off-board (laptop) rehearsal
```bash
python3 -m venv .venv && .venv/bin/pip install -r board/requirements.txt
cd board && ../.venv/bin/python -m pytest          # 14 tests
../.venv/bin/uvicorn guardian.app:app --port 8000  # all mocks; use the dashboard's rehearsal buttons
../.venv/bin/python tools/fake_watch.py            # optional: a typed stand-in for the watch
```

## Demo (about 2 minutes)
1. **False alarm rejected:** drop the watch on the table. The Pixels sweep amber, the camera sees a person standing, and the system goes back to green.
2. **Real fall:** lie on the mat. The watch vibrates and asks out loud, the Pixels count down, nobody taps, then the Pixels flash red and the
   caregiver page shows the description, snapshot and latency.
3. **I'm OK:** fall again and tap the watch. Everything clears.
4. **Privacy:** "Pause camera" on the caregiver page turns the MCU's camera light off. The watch still protects.

## Measurements (fill in on the board)

| Metric | Value |
|---|---|
| Vision check latency, p50 / p95 (`bench_vision.py`) | |
| Impact → check-in on the watch | |
| Check-in timeout → caregiver alert | 20 s by design |
| Fall test: falls caught / missed, false alarms (20 trials each) | |
| NPU delegation (`qnn_executor_runner --profile_level 1`) | |
| Peak RSS of the runner | |

## Privacy
Frames stay on the board. Only the alert snapshot is stored (set `keep_snapshots = false` to disable), in the
board's local care log. The camera-on light is driven by the MCU from the app's real camera state.

## Team
- Name, email
- Name, email
- Name, email

## License
MIT, see [LICENSE](LICENSE).
