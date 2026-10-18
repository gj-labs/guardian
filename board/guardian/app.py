"""Guardian board app: FastAPI hub tying the StopWatch, camera/NPU, STM32 Pixels and caregiver page.

Run on the VENTUNO Q (or a laptop with config defaults = all mocks):
    .venv/bin/uvicorn guardian.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config as config_mod
from .bridge import MockBridge, make_bridge
from .camera import Camera, jpeg
from .fusion import Effect, Fusion, FusionConfig, State
from .store import Store
from .tts import OUT_RATE, Speech
from .vision import MockVision, make_vision

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
        self.speech = Speech(t["piper_bin"] or None, t["piper_model"] or None, t["piper_rate"])
        self.privacy = False
        self.dash: set[WebSocket] = set()
        self.watch: WebSocket | None = None
        self.watch_seen = 0.0
        self.watch_battery: int | None = None
        self.vision_busy = False
        self.last_vision: dict | None = None
        self.last_bg = 0.0
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
            "tts": self.speech.engine,
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
        if self.watch is None:
            return
        pcm = await asyncio.to_thread(self.speech.pcm16k, text)
        if not pcm:
            return
        try:
            await self.watch.send_json({"type": "audio", "rate": OUT_RATE, "bytes": len(pcm)})
            await self.watch.send_bytes(pcm)
        except Exception:
            self.watch = None

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
                snap = self._snapshot() if kind == "caregiver_alert" else None
                eid = self.store.add(kind, data, snap)
                await self.broadcast({"type": "event", "event": {"id": eid, "t": time.time(), "kind": kind,
                                                                 "data": data, "has_snapshot": snap is not None}})
            elif fx.kind == "verify":
                asyncio.create_task(self.verify(fx.data["impact_t"]))

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

    async def verify(self, impact_t: float) -> None:
        frame = None
        if self.camera_active():
            got = self.camera.frame_after(impact_t + self.fusion.cfg.settle_s)
            frame = got[1] if got else None
        res = await self._check(frame)
        self.store.add("vision", {**res, "trigger": "impact"})
        await self.run(self.fusion.on_vision(time.monotonic(), res["on_floor"], res["description"]))

    async def background_check(self, now: float) -> None:
        latest = self.camera.latest() if self.camera_active() else None
        if latest is None and not isinstance(self.vision, MockVision):
            return
        self.last_bg = now
        res = await self._check(latest[1] if latest else None)
        if res["on_floor"]:
            self.store.add("vision", {**res, "trigger": "background"})
        await self.run(self.fusion.on_vision(time.monotonic(), res["on_floor"], res["description"],
                                             background=True))

    def _bg_due(self, now: float) -> bool:
        if self.fusion.state != State.MONITORING or self.vision_busy:
            return False
        since = now - self.last_bg
        if self.camera_active() and since > 5 and self.camera.burst_then_still(now):
            return True
        return since > self.cfg["vision"]["background_interval_s"]

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
            if self._bg_due(now):
                asyncio.create_task(self.background_check(now))
            await asyncio.sleep(0.25)

    async def start(self) -> None:
        if self.camera is not None:
            self.camera.start()
        for text in ("Are you OK? Tap the watch if you are fine.", "I have called for help. Help is on the way.",
                     "Okay. Glad you are fine."):
            await asyncio.to_thread(self.speech.pcm16k, text)   # warm the phrase cache
        self.store.add("startup", {"vision": self.vision.name, "tts": self.speech.engine,
                                   "bridge": type(self.bridge).__name__})
        self._tasks.append(asyncio.create_task(self.tick_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.camera is not None:
            self.camera.stop()


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

    @app.post("/api/ack")
    async def ack():
        await g.run(g.fusion.on_ok(time.monotonic(), by="caregiver"))
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

    @app.post("/api/sim/impact")
    async def sim_impact(peak_g: float = 3.5):
        dev_only()
        await g.run(g.fusion.on_impact(time.monotonic(), peak_g))
        return g.status()

    @app.post("/api/sim/ok")
    async def sim_ok():
        dev_only()
        await g.run(g.fusion.on_ok(time.monotonic()))
        return g.status()

    @app.post("/api/sim/sos")
    async def sim_sos():
        dev_only()
        await g.run(g.fusion.on_sos(time.monotonic()))
        return g.status()

    @app.post("/api/sim/floor")
    async def sim_floor(p: Toggle):
        dev_only()
        if not isinstance(g.vision, MockVision):
            raise HTTPException(409, "only with vision.backend = mock")
        g.vision.on_floor = p.on
        return g.status()

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
        await ws.accept()
        g.watch, g.watch_seen = ws, time.monotonic()
        g.store.add("watch_connected", {})
        await ws.send_json({"type": "state", "state": g.fusion.state.name,
                            "seconds": g.fusion.countdown_left(time.monotonic())})
        try:
            while True:
                msg = await ws.receive_json()
                g.watch_seen = time.monotonic()
                kind = msg.get("type")
                now = time.monotonic()
                if kind == "hb":
                    g.watch_battery = msg.get("battery")
                elif kind == "impact":
                    g.store.add("impact", {"peak_g": msg.get("peak_g")})
                    await g.run(g.fusion.on_impact(now, float(msg.get("peak_g", 0))))
                elif kind == "ok":
                    await g.run(g.fusion.on_ok(now))
                elif kind == "sos":
                    await g.run(g.fusion.on_sos(now))
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
