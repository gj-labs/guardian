import os
import socket
import tempfile
import threading

import msgpack
import numpy as np
import pytest

from guardian.bridge import RouterBridge, RpcError
from guardian.camera import Camera
from guardian.store import Store
from guardian.vision import EncoderVision, VlmVision, parse_yes_no, preprocess, _assistant_text


def fake_router(path, reply):
    """One-connection Unix-socket server that answers each request via reply(msg)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        unpacker = msgpack.Unpacker(raw=False)
        with conn:
            while data := conn.recv(4096):
                unpacker.feed(data)
                for msg in unpacker:
                    out = reply(msg)
                    # Split the reply in two writes: the client must use a streaming decoder.
                    conn.sendall(out[:2])
                    conn.sendall(out[2:])
        srv.close()

    threading.Thread(target=serve, daemon=True).start()


def test_bridge_call_roundtrip_and_error():
    d = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(d, "r.sock")
    seen = []

    def reply(msg):
        seen.append(msg)
        _, msgid, method, args = msg
        notify = msgpack.packb([2, "noise", []])          # unrelated notification first
        if method == "boom":
            return notify + msgpack.packb([1, msgid, "no such method", None])
        return notify + msgpack.packb([1, msgid, None, True])

    fake_router(path, reply)
    b = RouterBridge(path, timeout=2)
    assert b.call("guardian_set", 2, True, False, 15) is True
    assert seen[0][2:] == ["guardian_set", [2, True, False, 15]]
    with pytest.raises(RpcError):
        b.call("boom")


def test_store_hash_chain_detects_edits(tmp_path):
    s = Store(tmp_path / "g.sqlite")
    s.add("impact", {"peak_g": 3.1})
    eid = s.add("caregiver_alert", {"why": "no response"}, b"jpg")
    assert s.verify() and s.snapshot(eid) == b"jpg"
    s._db.execute("UPDATE events SET data='{}' WHERE id=1")
    assert not s.verify()


def test_vision_helpers():
    assert parse_yes_no("Yes. A person is lying by the couch.") is True
    assert parse_yes_no("no, the person is standing") is False
    assert parse_yes_no("unclear") is None
    out = "<|im_start|>user:\n<img></img>Is a person on the floor?<|im_end|>assistant\nYes, by the couch.<|im_end|>"
    assert _assistant_text(out) == "Yes, by the couch."
    x = preprocess(np.zeros((480, 640, 3), np.uint8))
    assert x.shape == (1, 3, 448, 448) and x.dtype == np.float32 and x.flags["C_CONTIGUOUS"]


def test_encoder_vision_nearest_reference(tmp_path):
    import cv2
    # Fake "embedding": mean colour. Floor refs are red, upright refs are blue.
    for label, bgr in {"floor": (0, 0, 255), "upright": (255, 0, 0)}.items():
        (tmp_path / label).mkdir()
        for i in range(3):
            img = np.full((60, 80, 3), bgr, np.uint8)
            img[:, :10] = i * 20
            cv2.imwrite(str(tmp_path / label / f"{i}.jpg"), img)
    embed = lambda frames: np.stack([f.reshape(-1, 3).mean(0).astype(np.float32) for f in frames])
    v = EncoderVision(str(tmp_path), embed_fn=embed)
    assert v.load_refs() == {"floor": 3, "upright": 3}
    assert v.check(np.full((60, 80, 3), (10, 10, 240), np.uint8)).on_floor is True
    assert v.check(np.full((60, 80, 3), (240, 10, 10), np.uint8)).on_floor is False


def test_motion_gate_burst_then_still():
    cam = Camera("none", fps=5)
    still = np.zeros((120, 160, 3), np.uint8)
    moving = np.full((120, 160, 3), 255, np.uint8)
    prev = still[:, :, 0]
    t = 0.0
    for i in range(10):                                   # 2 s of big motion
        f = moving if i % 2 else still
        cam.push(t, f, prev)
        prev = f[:, :, 0]
        t += 0.2
    for _ in range(15):                                   # 3 s of stillness
        cam.push(t, still, still[:, :, 0])
        t += 0.2
    assert cam.burst_then_still(t)
    assert not cam.burst_then_still(t + 20)               # burst too long ago


FAKE_RUNNER = """#!/usr/bin/env python3
import sys, time
args = sys.argv[1:]
out = args[args.index("--output_path") + 1]
print("READY", flush=True)
for n, line in enumerate(sys.stdin):
    raw, prompt, seq = line.rstrip("\\n").split("\\t")
    if raw.endswith("missing.raw"):
        print(f"RESULT_ERROR {n} no_raw_files", flush=True); continue
    if n == 2:  # simulate the runner dying on its third request
        sys.exit(1)
    open(out, "w").write("<|im_start|>user:<img></img>" + prompt + "<|im_end|>assistant\\nYes, a person is lying on the floor.<|im_end|>")
    print(f"RESULT_READY {n} tokens=9 tps=30", flush=True)
"""


def test_vlm_persistent_runner_protocol_and_restart(tmp_path):
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER)
    runner.chmod(0o755)
    v = VlmVision(str(runner), str(tmp_path), "InternVL3", os.path.relpath(tmp_path / "work"), timeout_s=10)
    assert v.check(np.zeros((48, 64, 3), np.uint8)).on_floor is None     # black frame: never guess
    frame = np.full((48, 64, 3), 120, np.uint8)
    r = v.check(frame)
    assert r.on_floor is True and r.description.startswith("Yes, a person")
    assert (tmp_path / "work" / "frame.raw").stat().st_size == 3 * 448 * 448 * 4
    assert v.check(frame).on_floor is True
    # third request makes the fake runner exit: the client restarts it and retries once
    assert v.check(frame).on_floor is True
    v.stop()


def test_thud_detector_fires_on_loud_transient_only():
    from guardian.audio import ThudDetector
    d = ThudDetector(rise_db=22, min_dbfs=-32, cooldown_s=5)
    t = 0.0
    for _ in range(100):                                   # quiet room around -55 dBFS
        assert not d.feed(t, -55.0)
        t += 0.05
    assert not d.feed(t, -45.0)                            # talking: +10 dB, no trigger
    assert d.feed(t + 0.05, -20.0)                         # thud: +35 dB
    assert not d.feed(t + 1.0, -20.0)                      # cooldown
    assert d.feed(t + 6.0, -20.0)


def test_twilio_sms_request_and_error_handling():
    import base64
    import json as _json
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from guardian.sms import TwilioSms, message

    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            got.append((self.path, self.headers["Authorization"], body))
            code = 400 if body["To"][0] == "+15550000002" else 201
            self.send_response(code)
            self.end_headers()
            self.wfile.write(_json.dumps({"sid": "SM123"}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sms = TwilioSms("+15550000000", ["+15550000001", "+15550000002"], "ACxx", "tok",
                    api_base=f"http://127.0.0.1:{srv.server_port}")
    text = message("Yes, the person is lying on the floor.", 75, "home")
    res = sms.send(text)
    srv.shutdown()
    assert res[0] == {"to": "+15550000001", "ok": True, "sid": "SM123"}
    assert res[1]["ok"] is False and "HTTP 400" in res[1]["error"]
    path, auth, body = got[0]
    assert path == "/2010-04-01/Accounts/ACxx/Messages.json"
    assert auth == "Basic " + base64.b64encode(b"ACxx:tok").decode()
    assert body["From"] == ["+15550000000"] and "possible fall" in body["Body"][0]
    assert not TwilioSms("+1", ["+2"], "", "").send("x")[0]["ok"]      # missing credentials: no request
