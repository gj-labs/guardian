# Guardian: private fall detection on Arduino VENTUNO Q

ExecuTorch Hackathon, IoT track. Guardian watches a room for falls, asks the person out loud
whether they're OK, and alerts a family member's watch only if they stay down. It needs no
wearable for the person at risk, and **video never leaves the house**.

**How it works:**
1. **Cheap triggers on the CPU, always on:** a loud **thud** on the USB camera's microphone,
   or **motion followed by stillness** in the camera view (what a fall looks like).
2. **NPU confirmation:** each trigger wakes one **InternVL3-1B** check on the Hexagon NPU (ExecuTorch + QNN):
   *"Is a person lying, sitting or collapsed on the floor?"*. Nobody on the floor means a false alarm, which is logged and ignored.
3. **Ask:** the board's speaker says *"Are you okay? … please get up"*. The NPU rechecks every 4 s.
   If the person gets up, the incident is cancelled.
4. **Alert:** still down after 20 s, the **family member's M5Stack StopWatch** vibrates, beeps and shows the
   model's description. Tapping *"I'm on my way"* makes the board tell the person that help is coming.
5. **STM32 + Modulino Pixels** show the state on the device itself. The microcontroller owns the light timing,
   a camera-on privacy light, and a fault light if the Linux app stops sending heartbeats.

```text
Brio camera ──frames──▶ motion-then-still (CPU) ─┐
Brio mic ─────audio──▶ thud detector (CPU) ──────┤ trigger
                                                 ▼
                     FastAPI app on VENTUNO Q (state machine, care log)
                         │ one stdin line per check       │ Arduino Bridge (msgpack-RPC)
                         ▼                                ▼
          qnn_multimodal_persistent (ExecuTorch)    STM32H5 ──I2C──▶ Modulino Pixels
                         │ QNN HTP / FastRPC
                         ▼
                  Hexagon NPU: InternVL3-1B
Board speaker ◀── espeak-ng / Piper (CPU)      Family StopWatch ◀── Wi-Fi WebSocket ──▶ "I'm on my way"
Caregiver page ◀── http://<board>:8000         Camera aiming page ◀── http://<board>:8000/aim
```

States: `MONITORING → VERIFYING (NPU looks) → CHECKIN (asks; 20 s, rechecks every 4 s) → ALERT (family watch)`.

| Pixels | Meaning |
|---|---|
| pixel 0 dim green | monitoring; pixel 6 yellow = family watch offline |
| amber sweep | NPU is checking a trigger |
| amber bar shrinking | asking the person to get up (countdown run by the MCU) |
| all red flashing | family alerted (latched until they answer or the person gets up) |
| pixel 0 blinking blue | Linux app not responding (MCU heartbeat timeout) |
| pixel 7 white | camera is on |

## Tech stack: Guardian as a body

> Cheap reflexes listen and watch all the time. The NPU brain wakes only when something happens, decides in
> about a second, and the body asks before it calls the family, all on one Qualcomm board with no cloud.

```text
        EYES  ─┐  Logitech Brio 101 camera
        EARS  ─┤  Brio microphone
               ▼
     REFLEXES  CPU checks (motion-then-stillness, thud)  ── cheap, always on
               │ wakes up
               ▼
        BRAIN  InternVL3-1B on the Hexagon NPU (ExecuTorch + QNN)  ── "Is someone on the floor?"
               │
               ▼
     JUDGMENT  Python state machine (FastAPI)  ── check → ask → recheck → alert
        │         │            │                 │
     MEMORY     VOICE     NERVOUS SYSTEM ──▶ FACE      MESSENGER
   care log   speaker    Bridge → STM32 → Pixels      family StopWatch
```

| Body part | What it is | Tech | Job |
|---|---|---|---|
| 🦴 **Body** | Arduino VENTUNO Q | Qualcomm Dragonwing IQ8 (QCS8275), Ubuntu 24.04, 16 GB RAM | Everything runs here, with no cloud |
| 👀 **Eyes** | USB camera | Logitech Brio 101, OpenCV at 5 fps | Watches the floor |
| 👂 **Ears** | The camera's microphone | 16 kHz audio via ALSA `arecord` | Listens for a thud |
| ⚡ **Reflexes** | Cheap triggers on the CPU | Frame differencing ("movement, then stillness"); a loudness jump of more than 22 dB | Wake the brain only when something happens. A doll that has lain still all along never triggers it |
| 🧠 **Brain** | Vision-language model on the NPU | **InternVL3-1B** for QCS8275 (3 `.pte` files: vision encoder, token embedding, decoder), run by ExecuTorch's `qnn_multimodal_persistent` on the **Hexagon NPU** (QNN 2.49 HTP, FastRPC) | Looks and answers in plain language: *"Yes, the person is lying on the floor."* About 1 s per check, kept loaded (8 s load once), 574 MiB |
| 🎯 **Judgment** | Decision logic | Python state machine in FastAPI: MONITORING → VERIFYING → CHECKIN → ALERT | Asks first, rechecks every 4 s, cancels if the person gets up, rejects false alarms |
| 🗣️ **Voice** | Spoken check-in | espeak-ng (or Piper) text-to-speech through PipeWire | *"Are you okay? … please get up."* |
| 🧬 **Nervous system** | Linux-to-microcontroller link | Arduino Bridge (MessagePack-RPC through `arduino-router`) to the **STM32H5** (Zephyr), heartbeat every second | Carries decisions to the hardware. If the brain stops, the MCU shows a fault |
| 🚨 **Face** | Status lights | **Modulino Pixels** (8 RGB LEDs over Qwiic/I2C), driven by the STM32 | Green = calm, amber = checking or countdown, red = family alerted, white = camera on |
| 📣 **Messenger** | The family's wrist | **M5Stack StopWatch** (ESP32-S3) over a Wi-Fi WebSocket; finds the board by name (`qcventuno001.local`) | Buzzes and shows what the camera saw; a tap means *"I'm on my way"* |
| 📒 **Memory** | Care log | SQLite with a SHA-256 hash chain; keeps only the alert snapshot | Tamper-evident history for the caregiver |
| 🪟 **Window** | Caregiver page | FastAPI web page plus a live `/aim` camera view | Live state, the NPU's answer, latency and the log |
| ❤️ **Immune system** | Self-healing | systemd user service, plus the STM32 app as the board's default app; dark-frame guard | Restarts on its own after a power cut. A dark picture never gets a guess; Guardian asks instead |

## Measured on the board (VENTUNO Q, QCS8275, QNN 2.49, ExecuTorch from the track archive)

| Metric | Value |
|---|---|
| InternVL3-1B load (once, kept warm) | 7.9 s cold, 2.3 s cached |
| One camera check (NPU) | **0.9–1.5 s** (prefill 0.86 s at 316 tok/s, decode 37 tok/s) |
| Trigger → spoken check-in | 2.7–2.8 s (sound trigger, including a 1.5 s settle) |
| Trigger → family alert (person stays down) | 22.5 s (20 s check-in by design) |
| Person gets up → incident cancelled | ≤ 6 s (NPU recheck every 4 s) |
| Runner memory | 574 MiB RSS |
| Empty floor | 1/1 "no" |
| Person standing | 5/5 "no" |
| Doll lying on the floor | 6/6 "yes": a known limitation, see below |
| Person lying on the open floor (full body, mounted camera) | **5/5 "yes"**, 1.1–2.4 s |
| Thud while the person is standing | rejected as a false alarm (NPU: "not lying down") |
| Power cycle | Guardian, the NPU runner and the STM32 app all restart on their own |

**Known limitation:** the 1B model treats a doll on the floor as a person, and stricter prompts made it worse
(7/12). Guardian handles this in the system design instead: camera triggers need motion followed by stillness,
so an object that has lain still all along never triggers a check. A real person also gets the spoken
check-in before anyone is alerted.

## Models and runtime

All are from the track's VENTUNO Q bundle (QCS8275, QNN).

| Use | File |
|---|---|
| Fall check | `InternVL3-1B_QCS8275_ctxt_1000/`: `.pte` decoder, `_vision_encoder_qnn.pte`, `_tok_embedding_qnn.pte`, `tokenizer.json` |
| Runner | `qnn_multimodal_persistent` from `ventuno_executorch_source.tar.gz` (extracted to `~/git/executorch`); loads the model once and serves one request per stdin line |
| QNN runtime | `libqnn1`, `libqnn-dev`, `qnn-tools` 2.49 from the Qualcomm Ubuntu PPA (`ppa:ubuntu-qcom-iot/qcom-ppa`) |
| Speech | espeak-ng (or Piper) on the CPU. These are not ExecuTorch models |

Preprocessing matches the exporter: 448×448 RGB, ImageNet mean/std, float32 NCHW, written to a `.raw` file
(`board/guardian/vision.py:preprocess`).

## Repository

| Path | What |
|---|---|
| `board/guardian/` | Linux app: `fusion.py` (state machine), `vision.py` (NPU runner client), `audio.py` (thud detector), `camera.py` (capture + motion gate), `bridge.py` (router RPC), `tts.py` (speaker), `store.py` (hash-chained care log), `app.py` (FastAPI), `static/` (caregiver and aim pages) |
| `board/tests/` | 17 tests: state machine, runner protocol with a fake runner, bridge protocol, thud detector, full app flow |
| `board/tools/` | `fake_watch.py`, `capture_refs.py`, `bench_vision.py` |
| `board/guardian.service` | systemd user service (starts at boot) |
| `mcu/guardian-mcu/` | Arduino App for the STM32: sketch (`guardian_set` over Bridge, Pixels) |
| `watch/guardian_watch/` | StopWatch firmware: the family alert |

## Setup

### 1. Board (VENTUNO Q, Ubuntu 24.04)
```bash
sudo add-apt-repository -y ppa:ubuntu-qcom-iot/qcom-ppa && sudo apt update
sudo apt install -y libqnn1 libqnn-dev qnn-tools python3.12-venv espeak-ng
tar -xzf ventuno_executorch_source.tar.gz -C ~          # -> ~/git/executorch (track bundle)
# copy InternVL3-1B_QCS8275_ctxt_1000/ to ~/models/
git clone https://github.com/gj-labs/guardian.git ~/guardian && cd ~/guardian/board
cp config.example.toml config.toml                       # check camera/mic/speaker devices
mkdir -p ~/.config/systemd/user && cp guardian.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now guardian
```
Open `http://<board-ip>:8000` (caregiver page) and `http://<board-ip>:8000/aim` to point the camera at the floor.
Device names: `arecord -l` (mic, e.g. `plughw:1,0` for the Brio 101), `wpctl status` (speaker: pair a
Bluetooth speaker or plug in a USB one; `speaker = "default"` uses PipeWire's default output).

### 2. STM32 + Pixels
Plug the Modulino Pixels into the Qwiic port, then on the board:
```bash
~/guardian/mcu/deploy.sh                                          # builds + flashes via arduino-app-cli
arduino-app-cli properties set default ~/ArduinoApps/guardian-mcu  # start at boot
```

### 3. Family StopWatch
On a computer with `arduino-cli`, the M5Stack board package and the libraries `M5Unified`, `WebSockets` and `ArduinoJson`:
copy `watch/guardian_watch/config.example.h` to `config.h` (Wi-Fi + board IP), then compile and upload for the StopWatch.

### Off-board rehearsal (laptop, all mocks)
```bash
python3 -m venv .venv && .venv/bin/pip install -r board/requirements.txt
cd board && ../.venv/bin/python -m pytest
../.venv/bin/uvicorn guardian.app:app --port 8000   # dashboard rehearsal buttons drive the flow
```

## Demo (about 2 minutes)
1. **False alarm:** drop a bag near someone standing. The mic triggers and the Pixels sweep amber; the NPU sees nobody on the floor, so it goes back to green and the watch shows "False alarm".
2. **Fall:** a teammate lies down on the mat. Guardian asks out loud, the Pixels count down, then the family watch buzzes with the model's
   description and the caregiver page shows the snapshot and latency.
3. **I'm on my way:** tap the watch. The board says help is coming.
4. **Gets up:** repeat, and stand up during the countdown. The NPU recheck cancels the incident.
5. **Privacy:** "Pause camera" turns off the MCU's camera light.

## Privacy
Camera frames and audio are processed in memory on the board and are not stored. Only the alert snapshot is kept
(`keep_snapshots = false` disables it), in a local, hash-chained care log. Nothing is sent to the cloud.

## Team
- Name, email
- Name, email
- Name, email

## License
MIT, see [LICENSE](LICENSE).
