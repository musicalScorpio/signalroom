"""SQLite operator projection for the local side-effect reliability lab."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from backend.legacy.core import DEFAULT_DB, utc_now


_LOCK = threading.RLock()


class ReliabilityStore:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS reliability_runs (
                    id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def create(self, record: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        record = copy.deepcopy(record)
        record.setdefault("created_at", now)
        record["updated_at"] = now
        with self.connect() as db:
            db.execute(
                "INSERT INTO reliability_runs VALUES (?, ?, ?, ?, ?, ?)",
                (record["id"], record["strategy"], record["status"], json.dumps(record), record["created_at"], now),
            )
        return record

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT record FROM reliability_runs WHERE id = ?", (run_id,)).fetchone()
        return json.loads(row["record"]) if row else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT record FROM reliability_runs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

    def mutate(self, run_id: str, operation: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with _LOCK:
            record = self.get(run_id)
            if not record:
                return None
            operation(record)
            record["updated_at"] = utc_now()
            with self.connect() as db:
                db.execute(
                    "UPDATE reliability_runs SET strategy = ?, status = ?, record = ?, updated_at = ? WHERE id = ?",
                    (record["strategy"], record["status"], json.dumps(record), record["updated_at"], run_id),
                )
            return record

    def patch(self, run_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        return self.mutate(run_id, lambda record: record.update(copy.deepcopy(changes)))

    def timeline(
        self,
        run_id: str,
        kind: str,
        title: str,
        detail: str = "",
        data: Any = None,
        status: str = "complete",
        event_id: str | None = None,
    ) -> dict[str, Any] | None:
        def append(record: dict[str, Any]) -> None:
            timeline = record.setdefault("timeline", [])
            if event_id and any(item.get("id") == event_id for item in timeline):
                return
            timeline.append({
                "id": event_id or f"step_{len(timeline) + 1:03d}",
                "kind": kind,
                "title": title,
                "detail": detail,
                "data": copy.deepcopy(data),
                "status": status,
                "at": utc_now(),
            })

        return self.mutate(run_id, append)


def reliability_store() -> ReliabilityStore:
    return ReliabilityStore(Path(os.getenv("SIGNALROOM_DB", str(DEFAULT_DB))))

