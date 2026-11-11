"""SQLite projection store for durable multi-agent incident cases."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from signalroom_core import DEFAULT_DB, utc_now


_LOCK = threading.RLock()


class CaseStore:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_cases (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    record TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = copy.deepcopy(record)
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.connect() as db:
            db.execute(
                "INSERT INTO agent_cases VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], record["status"], record["title"], json.dumps(record), record["created_at"], now),
            )
        return record

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT record FROM agent_cases WHERE id = ?", (case_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT record FROM agent_cases ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def mutate(self, case_id: str, operation: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with _LOCK:
            record = self.get(case_id)
            if not record:
                return None
            operation(record)
            record["updated_at"] = utc_now()
            with self.connect() as db:
                db.execute(
                    "UPDATE agent_cases SET status = ?, title = ?, record = ?, updated_at = ? WHERE id = ?",
                    (record["status"], record["title"], json.dumps(record), record["updated_at"], case_id),
                )
            return record

    def patch(self, case_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        return self.mutate(case_id, lambda record: record.update(copy.deepcopy(changes)))

    def timeline(self, case_id: str, kind: str, title: str, detail: str = "", data: Any = None, status: str = "complete") -> dict[str, Any] | None:
        def append(record: dict[str, Any]) -> None:
            record.setdefault("timeline", []).append({
                "id": f"step_{len(record.get('timeline', [])) + 1:03d}",
                "kind": kind,
                "title": title,
                "detail": detail,
                "data": data,
                "status": status,
                "at": utc_now(),
            })
        return self.mutate(case_id, append)

    def agent(self, case_id: str, agent_id: str, **changes: Any) -> dict[str, Any] | None:
        def update(record: dict[str, Any]) -> None:
            target = record.setdefault("agents", {}).setdefault(agent_id, {"id": agent_id, "status": "queued", "turn": 0, "observations": []})
            target.update(copy.deepcopy(changes))
            target["updated_at"] = utc_now()
        return self.mutate(case_id, update)


def case_store() -> CaseStore:
    import os
    return CaseStore(Path(os.getenv("SIGNALROOM_DB", str(DEFAULT_DB))))
