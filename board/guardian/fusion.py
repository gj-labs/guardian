"""Fall-alert state machine for a camera + microphone in the room.

Cheap CPU triggers (a loud thud on the mic, or motion followed by stillness on the camera)
wake the NPU, which checks whether someone is on the floor. The board's speaker asks the
person to get up; the NPU keeps rechecking, and if they stay down the family's StopWatch
gets the alert. Pure logic with an injected clock, so it can be unit-tested off the board.
Every input returns a list of Effects that the app executes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class State(IntEnum):
    MONITORING = 0
    VERIFYING = 1   # a trigger fired; the camera is looking
    CHECKIN = 2     # person on the floor: asking them to get up, rechecking
    ALERT = 3       # family alerted; latched until they acknowledge or the person gets up


@dataclass
class Effect:
    kind: str                 # "state" | "watch" | "speak" | "log" | "verify" | "sms"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FusionConfig:
    checkin_s: float = 20.0          # time the person has to get up before the family is alerted
    verify_timeout_s: float = 15.0   # camera must answer within this, else ask the person anyway
    settle_s: float = 1.5            # wait after a thud before looking (person comes to rest)
    recheck_s: float = 4.0           # how often the camera rechecks during the check-in and alert
    rearm_s: float = 10.0            # ignore triggers this long after a rejected (person upright) check
    escalate_s: float = 60.0         # alert unanswered this long -> SMS to the backup contacts (0 = off)


CHECKIN_TEXT = "Are you okay? I noticed a fall. If you are okay, please get up. Otherwise I will alert your family."
ALERT_TEXT = "I have alerted your family. Help is on the way."
ACK_TEXT = "Your family has seen the alert and is on the way."
CLEAR_TEXT = "Okay. Glad you are up."
PHRASES = (CHECKIN_TEXT, ALERT_TEXT, ACK_TEXT, CLEAR_TEXT)


class Fusion:
    def __init__(self, cfg: FusionConfig | None = None) -> None:
        self.cfg = cfg or FusionConfig()
        self.state = State.MONITORING
        self.since = 0.0
        self.incident: dict[str, Any] | None = None
        self._rearm_until = 0.0
        self._verify_at: float | None = None
        self._recheck_at: float | None = None
        self._escalated = False

    # ------------------------------------------------------------ helpers
    def _go(self, state: State, now: float, **log) -> list[Effect]:
        self.state, self.since = state, now
        effects = [Effect("state", {"state": state.name, "since": now, "incident": self.incident})]
        if log:
            effects.append(Effect("log", {"kind": f"state:{state.name}", **log}))
        return effects

    def countdown_left(self, now: float) -> float:
        if self.state != State.CHECKIN:
            return 0.0
        return max(0.0, self.cfg.checkin_s - (now - self.since))

    def _clear(self, now: float, reason: str, speak: str | None) -> list[Effect]:
        was = self.state.name
        self.incident = None
        self._recheck_at = None
        self._escalated = False
        fx = self._go(State.MONITORING, now, reason=reason, was=was)
        fx.append(Effect("watch", {"type": "clear", "text": reason}))
        if speak:
            fx.append(Effect("speak", {"text": speak}))
        return fx

    # ------------------------------------------------------------ inputs
    def on_trigger(self, now: float, source: str, **detail) -> list[Effect]:
        """A cheap CPU trigger fired: 'sound' (thud) or 'motion' (movement, then stillness)."""
        if self.state != State.MONITORING:
            return []
        if now < self._rearm_until:
            return [Effect("log", {"kind": "trigger_ignored", "source": source, "reason": "rearm", **detail})]
        self.incident = {"started": now, "source": source, **detail}
        # A thud happens at the moment of the fall, so give the person a moment to come to rest.
        # Motion-then-stillness already waited for that.
        self._verify_at = now + (self.cfg.settle_s if source == "sound" else 0.0)
        fx = self._go(State.VERIFYING, now, source=source, **detail)
        fx.append(Effect("watch", {"type": "checking", "text": f"Possible fall ({source}), checking"}))
        return fx

    def on_vision(self, now: float, on_floor: bool | None, description: str = "") -> list[Effect]:
        """Result of a camera check. on_floor=None means the check could not tell."""
        if self.state == State.VERIFYING:
            if self.incident is not None:
                self.incident["vision"] = {"on_floor": on_floor, "description": description,
                                           "latency_s": now - self.incident["started"]}
            if on_floor is False:
                self.incident = None
                self._rearm_until = now + self.cfg.rearm_s
                fx = self._go(State.MONITORING, now, reason="camera sees nobody on the floor",
                              description=description)
                fx.append(Effect("log", {"kind": "false_alarm_rejected", "description": description}))
                fx.append(Effect("watch", {"type": "clear", "text": "False alarm"}))
                return fx
            return self._checkin(now, "person on the floor" if on_floor else "camera could not tell")

        if self.state in (State.CHECKIN, State.ALERT):
            if on_floor is False:
                return self._clear(now, "person got up", CLEAR_TEXT)
            if self.incident is not None and on_floor and description:
                self.incident.setdefault("vision", {})["description"] = description
        return []

    def _checkin(self, now: float, reason: str) -> list[Effect]:
        self._recheck_at = now + self.cfg.recheck_s
        fx = self._go(State.CHECKIN, now, reason=reason)
        fx.append(Effect("speak", {"text": CHECKIN_TEXT}))
        return fx

    def on_family_ack(self, now: float) -> list[Effect]:
        """The family member tapped "I'm on my way" on the StopWatch (or the caregiver page)."""
        if self.state in (State.CHECKIN, State.ALERT, State.VERIFYING):
            return self._clear(now, "family is on the way", ACK_TEXT)
        return []

    def _alert(self, now: float, why: str) -> list[Effect]:
        fx = self._go(State.ALERT, now, reason=why)
        desc = ((self.incident or {}).get("vision") or {}).get("description") or "Possible fall."
        fx.append(Effect("watch", {"type": "alert", "text": desc}))
        fx.append(Effect("speak", {"text": ALERT_TEXT}))
        fx.append(Effect("log", {"kind": "family_alert", "why": why, "incident": self.incident}))
        return fx

    def tick(self, now: float) -> list[Effect]:
        if self.state == State.VERIFYING:
            if self._verify_at is not None and now >= self._verify_at:
                self._verify_at = None
                return [Effect("verify", {"after": (self.incident or {}).get("started", now)})]
            if now - self.since > self.cfg.verify_timeout_s:
                return self._checkin(now, "camera check timed out")
            return []
        if (self.state == State.ALERT and not self._escalated and self.cfg.escalate_s > 0
                and now - self.since >= self.cfg.escalate_s):
            self._escalated = True
            desc = ((self.incident or {}).get("vision") or {}).get("description") or "Possible fall."
            return [Effect("sms", {"text": desc, "unanswered_s": round(now - self.since)}),
                    Effect("log", {"kind": "escalated", "unanswered_s": round(now - self.since)})]
        if self.state == State.CHECKIN and self.countdown_left(now) <= 0:
            self._recheck_at = now + self.cfg.recheck_s
            return self._alert(now, "still on the floor after the check-in")
        if self.state in (State.CHECKIN, State.ALERT) and self._recheck_at is not None and now >= self._recheck_at:
            self._recheck_at = now + self.cfg.recheck_s
            return [Effect("verify", {"after": now, "recheck": True})]
        return []
