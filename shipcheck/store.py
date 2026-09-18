"""Tiny persistence layer: one JSON document per email in SQLite.

SQLite keeps the demo dependency-free; on Cloud Run point SHIPCHECK_DB at a
mounted volume (or swap this class for Firestore / Cloud SQL — the interface
is four methods).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS results (email_id TEXT PRIMARY KEY, doc TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, email_id TEXT, action TEXT, detail TEXT)")
            self._db.commit()

    def put(self, result: dict):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO results VALUES (?, ?)",
                             (result["email_id"], json.dumps(result, ensure_ascii=False)))
            self._db.commit()

    def get(self, email_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT doc FROM results WHERE email_id = ?", (email_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT doc FROM results ORDER BY email_id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM results").fetchone()[0]

    def log(self, at: str, email_id: str, action: str, detail: dict | str = ""):
        with self._lock:
            self._db.execute("INSERT INTO audit (at, email_id, action, detail) VALUES (?, ?, ?, ?)",
                             (at, email_id, action, json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail))
            self._db.commit()

    def audit(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT at, email_id, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"at": a, "email_id": e, "action": ac, "detail": d} for a, e, ac, d in rows]
