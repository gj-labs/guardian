"""End-to-end flow through the real FastAPI app with mocks for camera, NPU, MCU and TTS."""
import time

from fastapi.testclient import TestClient

from guardian import config
from guardian.app import create_app


def cfg(tmp_path, **fusion):
    c = config.load("/nonexistent.toml")
    c["data_dir"] = str(tmp_path)
    c["fusion"].update({"checkin_s": 1.0, "settle_s": 0.1, "rearm_s": 0.0, **fusion})
    c["vision"]["background_interval_s"] = 3600
    return c


def wait_state(client, want, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get("/api/status").json()
        if s["state"] == want:
            return s
        time.sleep(0.05)
    raise AssertionError(f"state never became {want}: {s['state']}")


def test_watch_fall_flow_alerts_and_drives_mcu(tmp_path):
    app = create_app(cfg(tmp_path))
    g = app.state.g
    with TestClient(app) as client, client.websocket_connect("/ws/watch") as watch:
        assert watch.receive_json()["type"] == "state"
        client.post("/api/sim/floor", json={"on": True})
        watch.send_json({"type": "hb", "battery": 80})
        watch.send_json({"type": "impact", "peak_g": 3.6})
        assert watch.receive_json()["type"] == "checkin"
        s = wait_state(client, "ALERT")
        assert s["incident"]["vision"]["on_floor"] is True
        assert s["watch"]["battery"] == 80
        msgs = []
        while not any(m.get("type") == "alert" for m in msgs):
            msgs.append(watch.receive_json())
        # The STM32 got the alert state (3) through the bridge.
        assert any(c[0] == "guardian_set" and c[1] == 3 for c in g.bridge.calls)
        client.post("/api/ack")
        wait_state(client, "MONITORING")
        ev = client.get("/api/events").json()
        assert ev["chain_ok"]
        assert {"impact", "vision", "caregiver_alert"} <= {e["kind"] for e in ev["events"]}


def test_dropped_watch_with_person_upright_is_rejected(tmp_path):
    app = create_app(cfg(tmp_path))
    with TestClient(app) as client, client.websocket_connect("/ws/watch") as watch:
        watch.receive_json()
        client.post("/api/sim/floor", json={"on": False})
        watch.send_json({"type": "impact", "peak_g": 4.0})
        wait_state(client, "VERIFYING", 2)
        wait_state(client, "MONITORING", 5)
        kinds = [e["kind"] for e in client.get("/api/events").json()["events"]]
        assert "false_alarm_rejected" in kinds and "caregiver_alert" not in kinds


def test_wearer_ok_from_watch(tmp_path):
    app = create_app(cfg(tmp_path, checkin_s=10.0))
    with TestClient(app) as client, client.websocket_connect("/ws/watch") as watch:
        watch.receive_json()
        client.post("/api/sim/floor", json={"on": True})
        watch.send_json({"type": "impact", "peak_g": 3.0})
        assert watch.receive_json()["type"] == "checkin"
        watch.send_json({"type": "ok"})
        wait_state(client, "MONITORING")


def test_camera_only_checks_need_motion_then_stillness(tmp_path):
    from guardian.app import Guardian

    class StubCam:
        active = True
        moved = False

        def burst_then_still(self, now):
            return self.moved

    g = Guardian(cfg(tmp_path))
    g.cfg["vision"]["background_interval_s"] = 0
    g.camera = StubCam()
    assert not g._bg_due(1000.0)          # static scene (e.g. a doll on the floor): never checked
    g.camera.moved = True
    assert g._bg_due(1000.0)              # something fell and went still: check it
    g.privacy = True
    assert not g._bg_due(1000.0)          # camera paused: no checks
