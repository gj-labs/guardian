"""Guardian board app: camera + mic triggers, NPU fall check, board speaker, STM32 Pixels,
and the family's StopWatch, tied together by a FastAPI hub with a caregiver page.

Run on the VENTUNO Q (or a laptop with config defaults = all mocks):
    .venv/bin/uvicorn guardian.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config as config_mod
from .bridge import MockBridge, make_bridge
from .audio import Microphone
from .camera import Camera, jpeg
from .fusion import PHRASES, Effect, Fusion, FusionConfig, State
from .store import Store
from .tts import Speech
from .vision import MockVision, make_vision, parse_yes_no

log = logging.getLogger("guardian")
STATIC = Path(__file__).parent / "static"


class Toggle(BaseModel):
    on: bool


class Guardian:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.fusion = Fusion(FusionConfig(**cfg["fusion"]))
        self.store = Store(Path(cfg["data_dir"]) / "guardian.sqlite")
        self.bridge = make_bridge(cfg["bridge"])
        cam = cfg["camera"]
        self.camera = None if str(cam["device"]) == "none" else Camera(
            cam["device"], cam["fps"], width=cam["width"], height=cam["height"])
        self.vision = make_vision(cfg["vision"])
        t = cfg["tts"]
        self.speech = Speech(t["piper_bin"] or None, t["piper_model"] or None, t["piper_rate"], t["speaker"])
        mic = cfg["mic"]
        self.mic = None if mic["device"] == "none" else Microphone(
            mic["device"], self._on_thud, rise_db=mic["rise_db"], min_dbfs=mic["min_dbfs"])
        self.loop: asyncio.AbstractEventLoop | None = None
        self.speaking = False
        self.privacy = False
        self.dash: set[WebSocket] = set()
        self.watch: WebSocket | None = None
        self.watch_seen = 0.0
        self.watch_battery: int | None = None
        self.vision_busy = False
        self.last_vision: dict | None = None
        self.last_motion_trigger = 0.0
        self._last_logged_check: tuple | None = None
        self.metrics: dict = {}
        self.mcu_error: str | None = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ status
    def watch_online(self) -> bool:
        return self.watch is not None and time.monotonic() - self.watch_seen < self.cfg["watch"]["offline_after_s"]

    def camera_active(self) -> bool:
        return self.camera is not None and self.camera.active and not self.privacy

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "type": "status",
            "state": self.fusion.state.name,
            "countdown": round(self.fusion.countdown_left(now), 1),
            "incident": self.fusion.incident,
            "watch": {"online": self.watch_online(), "battery": self.watch_battery},
            "camera": {"active": self.camera_active(), "privacy": self.privacy,
                       "error": None if self.camera is None else self.camera.error,
                       "enabled": self.camera is not None},
            "mcu": {"ok": bool(self.bridge.ok) and self.mcu_error is None, "mock": isinstance(self.bridge, MockBridge),
                    "error": self.mcu_error},
            "vision": {"backend": self.vision.name, "busy": self.vision_busy, "last": self.last_vision,
                       "mock_on_floor": getattr(self.vision, "on_floor", None)
                       if isinstance(self.vision, MockVision) else None},
            "mic": None if self.mic is None else {"active": self.mic.active, "error": self.mic.error,
                                                   "level_db": None if self.mic.level_db is None
                                                   else round(self.mic.level_db, 1)},
            "tts": self.speech.engine,
            "speaker": self.speech.speaker,
            "metrics": self.metrics,
            "dev": self.cfg["dev"],
        }

    # ------------------------------------------------------------ outputs
    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.dash):
            try:
                await ws.send_json(msg)
            except Exception:
                self.dash.discard(ws)

    async def to_watch(self, msg: dict) -> None:
        if self.watch is None:
            return
        try:
            await self.watch.send_json(msg)
        except Exception:
            self.watch = None

    async def speak(self, text: str) -> None:
        """Board speaker. The mic ignores its own voice while it plays."""
        self.speaking = True
        try:
            await asyncio.to_thread(self.speech.play, text)
        finally:
            self.speaking = False

    def _mcu_push(self) -> None:
        f = self.fusion
        try:
            self.bridge.call("guardian_set", int(f.state), self.camera_active(), self.watch_online(),
                             int(round(f.countdown_left(time.monotonic()))))
            self.mcu_error = None
        except Exception as e:  # board app keeps working if the MCU link drops
            self.mcu_error = str(e)

    async def run(self, effects: list[Effect]) -> None:
        for fx in effects:
            if fx.kind == "state":
                await asyncio.to_thread(self._mcu_push)
                self._mark(fx.data["state"])
                await self.broadcast(self.status())
            elif fx.kind == "watch":
                await self.to_watch(fx.data)
            elif fx.kind == "speak":
                asyncio.create_task(self.speak(fx.data["text"]))
            elif fx.kind == "log":
                data = dict(fx.data)
                kind = data.pop("kind")
                snap = self._snapshot() if kind == "family_alert" else None
                eid = self.store.add(kind, data, snap)
                await self.broadcast({"type": "event", "event": {"id": eid, "t": time.time(), "kind": kind,
                                                                 "data": data, "has_snapshot": snap is not None}})
            elif fx.kind == "verify":
                asyncio.create_task(self.verify(fx.data["after"], fx.data.get("recheck", False)))

    def _mark(self, state: str) -> None:
        """Latency metrics for the incident in progress (shown on the dashboard, used in the README)."""
        inc = self.fusion.incident
        if inc is None:
            return
        dt_ms = round((time.monotonic() - inc["started"]) * 1000)
        if state == "CHECKIN":
            self.metrics["trigger_to_checkin_ms"] = dt_ms
        elif state == "ALERT":
            self.metrics["trigger_to_alert_ms"] = dt_ms

    def _snapshot(self) -> bytes | None:
        if not self.camera_active() or not self.cfg["vision"]["keep_snapshots"]:
            return None
        latest = self.camera.latest()
        return jpeg(latest[1]) if latest else None

    # ------------------------------------------------------------ camera checks
    async def _check(self, frame) -> dict:
        self.vision_busy = True
        await self.broadcast(self.status())
        try:
            r = await asyncio.to_thread(self.vision.check, frame)
        finally:
            self.vision_busy = False
        self.last_vision = {"on_floor": r.on_floor, "description": r.description,
                            "latency_ms": round(r.latency_ms), "backend": r.backend, "t": time.time()}
        self.metrics["vision_ms"] = round(r.latency_ms)
        return self.last_vision

    async def verify(self, after: float, recheck: bool = False) -> None:
        if self.vision_busy:
            return
        frame = None
        if self.camera_active():
            got = self.camera.frame_after(after)
            frame = got[1] if got else None
        res = await self._check(frame)
        # Rechecks repeat every few seconds: log only when the answer changes.
        key = (res["on_floor"], res["description"])
        if not recheck or key != self._last_logged_check:
            self.store.add("vision", {**res, "trigger": "recheck" if recheck else "verify"})
        self._last_logged_check = key
        await self.run(self.fusion.on_vision(time.monotonic(), res["on_floor"], res["description"]))

    # ------------------------------------------------------------ CPU triggers
    def _on_thud(self, now: float, level_db: float) -> None:
        """Called from the mic thread."""
        if self.speaking or self.loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.trigger("sound", level_db=round(level_db, 1)), self.loop)

    async def trigger(self, source: str, **detail) -> None:
        if self.fusion.state == State.MONITORING:
            self.store.add("trigger", {"source": source, **detail})
        await self.run(self.fusion.on_trigger(time.monotonic(), source, **detail))

    def _motion_due(self, now: float) -> bool:
        # Movement followed by stillness is what a fall looks like on camera. Something that
        # has lain still all along (a doll, a bag) never fires this.
        return (self.fusion.state == State.MONITORING and not self.vision_busy and self.camera_active()
                and now - self.last_motion_trigger > 10 and self.camera.burst_then_still(now))

    # ------------------------------------------------------------ loops
    async def tick_loop(self) -> None:
        last_push = 0.0
        while True:
            now = time.monotonic()
            await self.run(self.fusion.tick(now))
            if now - last_push >= 1.0:          # heartbeat: MCU shows a fault if this stops
                last_push = now
                await asyncio.to_thread(self._mcu_push)
                await self.broadcast(self.status())
            if self._motion_due(now):
                self.last_motion_trigger = now
                asyncio.create_task(self.trigger("motion"))
            await asyncio.sleep(0.25)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        if self.camera is not None:
            self.camera.start()
        if self.mic is not None:
            self.mic.start()
        for text in PHRASES:
            await asyncio.to_thread(self.speech.pcm16k, text)   # warm the phrase cache
        if hasattr(self.vision, "start"):        # load the VLM once so the first fall check is fast
            try:
                await asyncio.to_thread(self.vision.start)
            except Exception as e:
                log.error("vision backend failed to start: %s", e)
                self.store.add("vision_start_failed", {"error": str(e)})
        self.store.add("startup", {"vision": self.vision.name, "tts": self.speech.engine,
                                   "bridge": type(self.bridge).__name__})
        self._tasks.append(asyncio.create_task(self.tick_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if hasattr(self.vision, "stop"):
            await asyncio.to_thread(self.vision.stop)
        if self.camera is not None:
            self.camera.stop()
        if self.mic is not None:
            self.mic.stop()


# ---------------------------------------------------------------- HTTP / WebSocket

def create_app(cfg: dict | None = None) -> FastAPI:
    g = Guardian(cfg or config_mod.load())

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await g.start()
        yield
        await g.stop()

    app = FastAPI(title="Guardian", lifespan=lifespan)
    app.state.g = g

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/aim")
    def aim():
        return FileResponse(STATIC / "aim.html")

    @app.get("/api/status")
    def status():
        return g.status()

    @app.get("/api/events")
    def events(limit: int = 100):
        return {"events": g.store.list(limit), "chain_ok": g.store.verify()}

    @app.get("/api/snapshot/{event_id}.jpg")
    def snapshot(event_id: int):
        snap = g.store.snapshot(event_id)
        if not snap:
            raise HTTPException(404)
        return Response(snap, media_type="image/jpeg")

    @app.get("/api/frame.jpg")
    def frame():
        """Latest camera frame, for aiming the camera (served only on the home network)."""
        latest = g.camera.latest() if g.camera_active() else None
        if latest is None:
            raise HTTPException(404, "camera off")
        return Response(jpeg(latest[1], 640), media_type="image/jpeg")

    @app.post("/api/ack")
    async def ack():
        await g.run(g.fusion.on_family_ack(time.monotonic()))
        return g.status()

    @app.post("/api/privacy")
    async def privacy(p: Toggle):
        g.privacy = p.on
        if g.camera is not None:
            await asyncio.to_thread(g.camera.stop if p.on else g.camera.start)
        g.store.add("privacy", {"on": p.on})
        await asyncio.to_thread(g._mcu_push)
        return g.status()

    # -------- rehearsal helpers (dev = true): drive the flow without the watch or a real fall
    def dev_only():
        if not g.cfg["dev"]:
            raise HTTPException(403, "dev endpoints disabled")

    @app.post("/api/sim/trigger")
    async def sim_trigger(source: str = "sound"):
        dev_only()
        await g.trigger(source, simulated=True)
        return g.status()

    @app.post("/api/sim/say")
    async def sim_say(text: str = "Hello. This is Guardian. Speaker test."):
        dev_only()
        await g.speak(text)
        return {"engine": g.speech.engine, "speaker": g.speech.speaker}

    @app.post("/api/sim/floor")
    async def sim_floor(p: Toggle):
        dev_only()
        if not isinstance(g.vision, MockVision):
            raise HTTPException(409, "only with vision.backend = mock")
        g.vision.on_floor = p.on
        return g.status()

    @app.post("/api/sim/check")
    async def sim_check(request: Request, prompt: str | None = None):
        """Run one camera check on an uploaded JPEG/PNG (body = image bytes). Does not change state.
        `prompt` (dev only) overrides the question, for tuning it on saved frames."""
        dev_only()
        import cv2
        import numpy as np
        frame = cv2.imdecode(np.frombuffer(await request.body(), np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "body must be a JPEG or PNG image")
        if prompt and hasattr(g.vision, "ask"):
            t0 = time.monotonic()
            text = await asyncio.to_thread(g.vision.ask, frame, prompt)
            return {"on_floor": parse_yes_no(text), "description": text,
                    "latency_ms": round((time.monotonic() - t0) * 1000), "prompt": prompt}
        return await g._check(frame)

    @app.websocket("/ws/dash")
    async def ws_dash(ws: WebSocket):
        await ws.accept()
        g.dash.add(ws)
        await ws.send_json(g.status())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            g.dash.discard(ws)

    @app.websocket("/ws/watch")
    async def ws_watch(ws: WebSocket):
        """The family member's StopWatch: shows alerts, sends "I'm on my way"."""
        await ws.accept()
        g.watch, g.watch_seen = ws, time.monotonic()
        g.store.add("watch_connected", {})
        inc = g.fusion.incident or {}
        await ws.send_json({"type": "state", "state": g.fusion.state.name,
                            "text": ((inc.get("vision") or {}).get("description") or "")})
        try:
            while True:
                msg = await ws.receive_json()
                g.watch_seen = time.monotonic()
                kind = msg.get("type")
                if kind == "hb":
                    g.watch_battery = msg.get("battery")
                elif kind == "ack":
                    await g.run(g.fusion.on_family_ack(time.monotonic()))
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            if g.watch is ws:
                g.watch = None
                g.store.add("watch_disconnected", {})

    return app


def __getattr__(name: str):
    # `uvicorn guardian.app:app` builds the app on first access, so tests can import
    # this module and call create_app(cfg) without touching the default data dir.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
