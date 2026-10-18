"""SMS escalation when nobody answers the family alert.

Text only: a short description and the time. No image or audio ever leaves the house.
Uses Twilio's REST API (https://www.twilio.com/docs/messaging/api/message-resource).
Credentials come from environment variables on the board, never from the repo:
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("guardian.sms")


def message(description: str, unanswered_s: int, place: str) -> str:
    when = dt.datetime.now().strftime("%H:%M")
    return (f"Guardian alert ({place}, {when}): possible fall, nobody has answered for {unanswered_s // 60 or 1} min. "
            f"Camera: {description[:120]} Please check on them now.")


class TwilioSms:
    name = "twilio"

    def __init__(self, from_number: str, to_numbers: list[str], account_sid: str | None = None,
                 auth_token: str | None = None, api_base: str = "https://api.twilio.com") -> None:
        self.from_number, self.to_numbers = from_number, to_numbers
        self.sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.api_base = api_base.rstrip("/")

    @property
    def ready(self) -> bool:
        return bool(self.sid and self.token and self.from_number and self.to_numbers)

    def send(self, text: str) -> list[dict]:
        """Send to every number; returns one result per recipient (never raises)."""
        if not self.ready:
            return [{"to": n, "ok": False, "error": "SMS not configured (TWILIO_* env vars, from, to)"}
                    for n in self.to_numbers or ["-"]]
        url = f"{self.api_base}/2010-04-01/Accounts/{self.sid}/Messages.json"
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        results = []
        for to in self.to_numbers:
            body = urllib.parse.urlencode({"From": self.from_number, "To": to, "Body": text}).encode()
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Authorization": f"Basic {auth}",
                                                  "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    results.append({"to": to, "ok": True, "sid": json.load(r).get("sid")})
            except urllib.error.HTTPError as e:
                results.append({"to": to, "ok": False, "error": f"HTTP {e.code}: {e.read()[:200]!r}"})
            except (urllib.error.URLError, OSError, ValueError) as e:
                results.append({"to": to, "ok": False, "error": str(e)})
        return results


class LogSms:
    """No provider configured: record what would have been sent (rehearsal, tests)."""

    name = "log"
    ready = True

    def __init__(self, to_numbers: list[str] | None = None) -> None:
        self.to_numbers = to_numbers or ["(no number set)"]
        self.sent: list[str] = []

    def send(self, text: str) -> list[dict]:
        self.sent.append(text)
        log.warning("SMS (not sent, provider=log): %s", text)
        return [{"to": n, "ok": True, "simulated": True} for n in self.to_numbers]


def make_sms(cfg: dict):
    if cfg.get("provider") == "twilio":
        return TwilioSms(cfg.get("from", ""), list(cfg.get("to", [])), api_base=cfg.get("api_base", "https://api.twilio.com"))
    return LogSms(list(cfg.get("to", [])))
