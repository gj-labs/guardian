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
from guardian.vision import EncoderVision, parse_yes_no, preprocess, _clean_answer


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
    out = "I 00:00 loading model\nprompt: ...\nASSISTANT: Yes, someone is on the floor.\nI 00:03 done"
    assert _clean_answer(out, "ASSISTANT:").startswith("Yes, someone")
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
