"""Fall-alert state machine: fuses wrist impacts with camera checks.

Pure logic with an injected clock, so it can be unit-tested off the board.
Every input returns a list of Effects that the app executes (MCU state,
watch messages, speech, dashboard broadcast, log entries).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class State(IntEnum):
    MONITORING = 0
    VERIFYING = 1   # impact seen, waiting for the camera to look
    CHECKIN = 2     # asking the wearer "are you OK?"
    ALERT = 3       # caregiver notified, latched until acknowledged or wearer says OK


@dataclass
class Effect:
    kind: str                 # "state" | "watch" | "speak" | "log" | "verify"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FusionConfig:
    checkin_s: float = 20.0          # time the wearer has to tap "I'm OK"
    verify_timeout_s: float = 8.0    # camera must answer within this, else ask the wearer anyway
    settle_s: float = 1.5            # wait after an impact before looking (person comes to rest)
    floor_confirmations: int = 2     # background checks in a row before a camera-only check-in
    rearm_s: float = 10.0            # ignore new impacts this long after a rejected false alarm


CHECKIN_TEXT = "Are you OK? Tap the watch if you are fine."
ALERT_TEXT = "I have called for help. Help is on the way."
CLEAR_TEXT = "Okay. Glad you are fine."


class Fusion:
    def __init__(self, cfg: FusionConfig | None = None) -> None:
        self.cfg = cfg or FusionConfig()
        self.state = State.MONITORING
        self.since = 0.0
        self.incident: dict[str, Any] | None = None
        self._floor_streak = 0
        self._rearm_until = 0.0
        self._verify_at: float | None = None

    # ------------------------------------------------------------ helpers
    def _go(self, state: State, now: float, **log) -> list[Effect]:
        self.state, self.since = state, now
        effects = [Effect("state", {"state": state.name, "since": now, "incident": self.incident})]
        if log:
            effects.append(Effect("log", {"kind": f"state:{state.name}", **log}))
        return effects

    def _start_incident(self, now: float, source: str, **extra) -> None:
        self.incident = {"started": now, "source": source, **extra}

    def countdown_left(self, now: float) -> float:
        if self.state != State.CHECKIN:
            return 0.0
        return max(0.0, self.cfg.checkin_s - (now - self.since))

    def _checkin(self, now: float, reason: str) -> list[Effect]:
        fx = self._go(State.CHECKIN, now, reason=reason)
        fx.append(Effect("watch", {"type": "checkin", "seconds": self.cfg.checkin_s, "text": CHECKIN_TEXT}))
        fx.append(Effect("speak", {"text": CHECKIN_TEXT}))
        return fx

    # ------------------------------------------------------------ inputs
    def on_impact(self, now: float, peak_g: float = 0.0) -> list[Effect]:
        if self.state != State.MONITORING:
            return [Effect("log", {"kind": "impact_ignored", "peak_g": peak_g, "state": self.state.name})]
        if now < self._rearm_until:
            return [Effect("log", {"kind": "impact_ignored", "peak_g": peak_g, "reason": "rearm"})]
        self._start_incident(now, "watch", peak_g=peak_g)
        self._verify_at = now + self.cfg.settle_s
        return self._go(State.VERIFYING, now, peak_g=peak_g)

    def on_sos(self, now: float) -> list[Effect]:
        """Wearer held the SOS button: skip straight to the alert."""
        if self.state == State.ALERT:
            return []
        self._start_incident(now, "sos")
        return self._alert(now, "SOS button held on the watch.")

    def on_vision(self, now: float, on_floor: bool | None, description: str = "",
                  background: bool = False) -> list[Effect]:
        """Result of a camera check. on_floor=None means the check failed."""
        if self.state == State.VERIFYING and not background:
            if self.incident is not None:
                self.incident["vision"] = {"on_floor": on_floor, "description": description,
                                           "latency_s": now - self.incident["started"]}
            if on_floor is False:
                self.incident = None
                self._rearm_until = now + self.cfg.rearm_s
                fx = self._go(State.MONITORING, now, reason="camera sees person upright",
                              description=description)
                fx.append(Effect("log", {"kind": "false_alarm_rejected", "description": description}))
                return fx
            reason = "camera confirms person on floor" if on_floor else "camera check failed"
            return self._checkin(now, reason)

        if background and self.state == State.MONITORING:
            self._floor_streak = self._floor_streak + 1 if on_floor else 0
            if self._floor_streak >= self.cfg.floor_confirmations:
                self._floor_streak = 0
                self._start_incident(now, "camera", vision={"on_floor": True, "description": description})
                return self._checkin(now, "camera sees person on floor (no watch impact)")
        return []

    def on_ok(self, now: float, by: str = "wearer") -> list[Effect]:
        if self.state in (State.CHECKIN, State.ALERT, State.VERIFYING):
            was = self.state.name
            self.incident = None
            fx = self._go(State.MONITORING, now, reason=f"cleared by {by}", was=was)
            fx.append(Effect("watch", {"type": "clear", "text": CLEAR_TEXT}))
            if by == "wearer":
                fx.append(Effect("speak", {"text": CLEAR_TEXT}))
            return fx
        return []

    def _alert(self, now: float, why: str) -> list[Effect]:
        fx = self._go(State.ALERT, now, reason=why)
        fx.append(Effect("watch", {"type": "alert", "text": ALERT_TEXT}))
        fx.append(Effect("speak", {"text": ALERT_TEXT}))
        fx.append(Effect("log", {"kind": "caregiver_alert", "why": why, "incident": self.incident}))
        return fx

    def tick(self, now: float) -> list[Effect]:
        if self.state == State.VERIFYING:
            if self._verify_at is not None and now >= self._verify_at:
                self._verify_at = None
                return [Effect("verify", {"impact_t": self.incident["started"] if self.incident else now})]
            if now - self.since > self.cfg.verify_timeout_s:
                return self._checkin(now, "camera check timed out")
        elif self.state == State.CHECKIN and self.countdown_left(now) <= 0:
            return self._alert(now, "no response to check-in")
        return []
