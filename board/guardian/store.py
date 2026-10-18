"""Care log in SQLite, hash-chained so edits after the fact are detectable
(same idea as BindLab's evidence log)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        self._db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, kind TEXT, data TEXT,
            snapshot BLOB, prev_hash TEXT, hash TEXT)""")
        self._db.commit()

    def _last_hash(self) -> str:
        row = self._db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row else "0" * 64

    def add(self, kind: str, data: dict | None = None, snapshot: bytes | None = None,
            t: float | None = None) -> int:
        t = time.time() if t is None else t
        body = json.dumps(data or {}, sort_keys=True, default=str)
        with self._lock:
            prev = self._last_hash()
            h = hashlib.sha256(f"{prev}|{t:.3f}|{kind}|{body}".encode()).hexdigest()
            cur = self._db.execute(
                "INSERT INTO events (t, kind, data, snapshot, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (t, kind, body, snapshot, prev, h))
            self._db.commit()
            return cur.lastrowid

    def list(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, t, kind, data, snapshot IS NOT NULL FROM events ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": i, "t": t, "kind": k, "data": json.loads(d), "has_snapshot": bool(s)}
                for i, t, k, d, s in rows]

    def snapshot(self, event_id: int) -> bytes | None:
        with self._lock:
            row = self._db.execute("SELECT snapshot FROM events WHERE id=?", (event_id,)).fetchone()
        return row[0] if row else None

    def verify(self) -> bool:
        prev = "0" * 64
        with self._lock:
            rows = self._db.execute("SELECT t, kind, data, prev_hash, hash FROM events ORDER BY id").fetchall()
        for t, kind, body, p, h in rows:
            if p != prev or hashlib.sha256(f"{prev}|{t:.3f}|{kind}|{body}".encode()).hexdigest() != h:
                return False
            prev = h
        return True
