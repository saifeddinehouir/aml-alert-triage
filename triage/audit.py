"""Append-only, hash-chained audit log in SQLite.

Each row stores sha256(previous_hash + canonical JSON of the event). Changing or
deleting any past row breaks every hash after it, and verify() says where.
UPDATE and DELETE are also blocked by triggers, so tampering needs direct file edits.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    case_id TEXT,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def _digest(prev: str, ts: str, actor: str, action: str, case_id: str | None, payload: str) -> str:
    body = json.dumps([prev, ts, actor, action, case_id, payload], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str = "audit.sqlite3"):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def append(self, actor: str, action: str, payload: dict, case_id: str | None = None) -> str:
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock, self._conn() as c:
            row = c.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row[0] if row else GENESIS
            ts = datetime.now(timezone.utc).isoformat()
            h = _digest(prev, ts, actor, action, case_id, data)
            c.execute("INSERT INTO audit_log (ts, actor, action, case_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                      (ts, actor, action, case_id, data, prev, h))
        return h

    def events(self, case_id: str | None = None) -> list[dict]:
        q = "SELECT seq, ts, actor, action, case_id, payload, hash FROM audit_log"
        args: tuple = ()
        if case_id:
            q += " WHERE case_id = ?"
            args = (case_id,)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY seq", args).fetchall()
        return [{"seq": r[0], "ts": r[1], "actor": r[2], "action": r[3], "case_id": r[4],
                 "payload": json.loads(r[5]), "hash": r[6]} for r in rows]

    def verify(self) -> dict:
        prev = GENESIS
        with self._conn() as c:
            rows = c.execute("SELECT seq, ts, actor, action, case_id, payload, prev_hash, hash FROM audit_log ORDER BY seq").fetchall()
        for seq, ts, actor, action, case_id, payload, prev_hash, h in rows:
            if prev_hash != prev or _digest(prev, ts, actor, action, case_id, payload) != h:
                return {"ok": False, "broken_at_seq": seq, "events_checked": seq - 1}
            prev = h
        return {"ok": True, "events_checked": len(rows), "head": prev}
