#!/usr/bin/env python3
"""SignalRoom: a dependency-free, local-first ambient agent control room."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DEFAULT_DB = ROOT / "signalroom.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Recommendation:
    label: str
    rationale: str
    risk: str
    confidence: float
    reversible: bool
    owner: str


RULES: dict[str, dict[str, Any]] = {
    "workflow.stalled": {
        "context": ["workflow owner", "elapsed time", "last successful step", "SLA"],
        "action": Recommendation(
            "Open an escalation and notify the workflow owner",
            "The workflow is outside its expected completion window and has no active recovery attempt.",
            "medium", 0.91, True, "Operations",
        ),
    },
    "threshold.breached": {
        "context": ["sensor history", "asset criticality", "maintenance window", "runbook"],
        "action": Recommendation(
            "Quarantine the affected asset and dispatch an inspection",
            "The observed value is beyond the policy limit and the asset is marked production-critical.",
            "high", 0.87, False, "Site reliability",
        ),
    },
    "document.uploaded": {
        "context": ["document classification", "retention policy", "account region", "prior reviews"],
        "action": Recommendation(
            "Route the document to the compliance review queue",
            "The document contains regulated fields and should enter the existing review process.",
            "low", 0.96, True, "Compliance",
        ),
    },
    "support.sla_risk": {
        "context": ["ticket history", "customer tier", "sentiment", "SLA clock"],
        "action": Recommendation(
            "Draft a priority response and assign a senior responder",
            "The ticket is approaching breach and the latest customer message indicates rising urgency.",
            "medium", 0.89, True, "Customer support",
        ),
    },
    "deploy.failed": {
        "context": ["deploy diff", "error signature", "recent incidents", "service ownership"],
        "action": Recommendation(
            "Create an incident with a rollback recommendation",
            "The failure matches a recent regression signature and is affecting a customer-facing service.",
            "high", 0.93, False, "Engineering",
        ),
    },
}


SEED_EVENTS = [
    {
        "id": "evt_7f31",
        "type": "deploy.failed",
        "source": "buildkite.prod",
        "summary": "Checkout API deployment failed after schema migration",
        "payload": {"service": "checkout-api", "commit": "7e19b2c", "region": "us-east-1"},
        "received_at": "2026-11-09T14:42:17+00:00",
    },
    {
        "id": "evt_441a",
        "type": "support.sla_risk",
        "source": "support.queue",
        "summary": "Enterprise ticket is 23 minutes from SLA breach",
        "payload": {"ticket": "T-1842", "tier": "enterprise", "minutes_remaining": 23},
        "received_at": "2026-11-09T14:38:03+00:00",
    },
    {
        "id": "evt_b902",
        "type": "document.uploaded",
        "source": "contracts.inbox",
        "summary": "New vendor agreement contains regulated data fields",
        "payload": {"document": "vendor-agreement.pdf", "region": "EU", "pages": 18},
        "received_at": "2026-11-09T14:31:49+00:00",
    },
    {
        "id": "evt_25c8",
        "type": "workflow.stalled",
        "source": "finance.close",
        "summary": "Month-end reconciliation has not advanced for 74 minutes",
        "payload": {"workflow": "nov-close", "stalled_minutes": 74, "sla_minutes": 45},
        "received_at": "2026-11-09T14:24:11+00:00",
    },
]


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    context TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    model_mode TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO settings(key, value) VALUES ('run_mode', 'shadow');
                """
            )

    def setting(self, key: str, fallback: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else fallback

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def put(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self.connect() as db:
            existing = db.execute("SELECT * FROM events WHERE id = ?", (record["id"],)).fetchone()
            if existing:
                return deserialize(existing), True
            db.execute(
                """
                INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["id"], record["type"], record["source"], record["summary"],
                    json.dumps(record["payload"]), record["received_at"], record["updated_at"],
                    json.dumps(record["context"]), json.dumps(record["policy"]),
                    json.dumps(record["decision"]), record["status"], json.dumps(record["outcome"]),
                    json.dumps(record["feedback"]), record["model_mode"],
                ),
            )
        return record, False

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY received_at DESC").fetchall()
        return [deserialize(row) for row in rows]

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return deserialize(row) if row else None

    def update(self, event_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {"status", "outcome", "feedback", "decision", "updated_at"}
        changes = {k: v for k, v in changes.items() if k in allowed}
        if not changes:
            return self.get(event_id)
        encoded = {k: json.dumps(v) if k in {"outcome", "feedback", "decision"} else v for k, v in changes.items()}
        setters = ", ".join(f"{key} = ?" for key in encoded)
        with self.connect() as db:
            db.execute(f"UPDATE events SET {setters} WHERE id = ?", (*encoded.values(), event_id))
        return self.get(event_id)


def deserialize(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "type": row["event_type"], "source": row["source"],
        "summary": row["summary"], "payload": json.loads(row["payload"]),
        "received_at": row["received_at"], "updated_at": row["updated_at"],
        "context": json.loads(row["context"]), "policy": json.loads(row["policy"]),
        "decision": json.loads(row["decision"]), "status": row["status"],
        "outcome": json.loads(row["outcome"]), "feedback": json.loads(row["feedback"]),
        "model_mode": row["model_mode"],
    }


class Engine:
    def __init__(self, store: Store):
        self.store = store

    def process(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        event_type = event.get("type", "workflow.stalled")
        rule = RULES.get(event_type, RULES["workflow.stalled"])
        recommendation: Recommendation = rule["action"]
        run_mode = self.store.setting("run_mode", "shadow")

        model_decision, model_mode = self._ask_local_model(event, rule)
        if model_decision:
            recommendation = Recommendation(
                model_decision.get("label", recommendation.label),
                model_decision.get("rationale", recommendation.rationale),
                model_decision.get("risk", recommendation.risk),
                float(model_decision.get("confidence", recommendation.confidence)),
                bool(model_decision.get("reversible", recommendation.reversible)),
                model_decision.get("owner", recommendation.owner),
            )

        requires_human = (
            recommendation.risk == "high"
            or not recommendation.reversible
            or (run_mode == "guarded" and recommendation.risk == "medium")
        )
        if run_mode == "shadow":
            status = "shadowed"
        elif requires_human:
            status = "needs_approval"
        else:
            status = "acted"

        now = event.get("received_at") or utc_now()
        trace = [
            {"stage": "event", "state": "complete", "at": now, "note": f"Accepted from {event.get('source', 'manual.input')}"},
            {"stage": "context", "state": "complete", "at": now, "note": f"Snapshot assembled from {len(rule['context'])} sources"},
            {"stage": "policy", "state": "complete", "at": now, "note": f"{recommendation.risk.title()} risk · {'reversible' if recommendation.reversible else 'irreversible'}"},
            {"stage": "action", "state": "complete", "at": now, "note": recommendation.label},
            {"stage": "human", "state": "waiting" if status == "needs_approval" else "bypassed", "at": now, "note": "Approval required" if requires_human else "Within delegated authority"},
            {"stage": "outcome", "state": "shadow" if status == "shadowed" else "waiting", "at": now, "note": "No side effect in shadow mode" if status == "shadowed" else "Awaiting outcome"},
            {"stage": "feedback", "state": "waiting", "at": now, "note": "Unrated"},
        ]
        record = {
            "id": event.get("id") or f"evt_{uuid.uuid4().hex[:8]}",
            "type": event_type,
            "source": event.get("source", "manual.input"),
            "summary": event.get("summary", "An event arrived without a summary"),
            "payload": event.get("payload", {}),
            "received_at": now,
            "updated_at": utc_now(),
            "context": {"snapshot": rule["context"], "freshness": "current", "captured_at": utc_now()},
            "policy": {"run_mode": run_mode, "requires_human": requires_human, "risk": recommendation.risk, "reversible": recommendation.reversible},
            "decision": {**asdict(recommendation), "trace": trace},
            "status": status,
            "outcome": {"state": "observed_only" if status == "shadowed" else "pending", "note": "No external tool was called."},
            "feedback": {},
            "model_mode": model_mode,
        }
        return self.store.put(record)

    def _ask_local_model(self, event: dict[str, Any], rule: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if os.getenv("SIGNALROOM_MODEL", "rules") != "ollama":
            return None, "transparent-rules"
        endpoint = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
        model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        prompt = (
            "You are a bounded event triage agent. Return JSON only with keys label, rationale, "
            "risk (low|medium|high), confidence (0..1), reversible (boolean), owner. "
            "Prefer no action when the signal is weak. Event: " + json.dumps(event) +
            ". Available context: " + json.dumps(rule["context"])
        )
        body = json.dumps({
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.1},
        }).encode()
        request = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read())
                content = result.get("message", {}).get("content", "{}")
                return json.loads(content), f"ollama/{model}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            return None, f"transparent-rules (local model unavailable: {model})"


class SignalRoomHandler(SimpleHTTPRequestHandler):
    store: Store
    engine: Engine

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/events":
            self._json({"events": self.store.list(), "mode": self.store.setting("run_mode", "shadow")})
            return
        if path == "/api/metrics":
            events = self.store.list()
            useful = sum(1 for item in events if item["feedback"].get("rating") == "useful")
            noisy = sum(1 for item in events if item["feedback"].get("rating") == "noise")
            self._json({
                "total": len(events), "useful": useful, "noise": noisy,
                "escalations": sum(1 for item in events if item["policy"]["requires_human"]),
                "shadowed": sum(1 for item in events if item["status"] == "shadowed"),
                "acted": sum(1 for item in events if item["status"] in {"acted", "approved"}),
            })
            return
        if path.startswith("/api/events/"):
            event = self.store.get(path.rsplit("/", 1)[-1])
            self._json(event if event else {"error": "Event not found"}, HTTPStatus.OK if event else HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._body()
        except json.JSONDecodeError:
            self._json({"error": "Invalid JSON"}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/events":
            event, duplicate = self.engine.process(body)
            self._json({"event": event, "duplicate": duplicate}, HTTPStatus.OK if duplicate else HTTPStatus.CREATED)
            return
        if path == "/api/config":
            mode = body.get("run_mode")
            if mode not in {"shadow", "guarded", "autonomous"}:
                self._json({"error": "run_mode must be shadow, guarded, or autonomous"}, HTTPStatus.BAD_REQUEST)
                return
            self.store.set_setting("run_mode", mode)
            self._json({"run_mode": mode})
            return
        if path.endswith("/approve") or path.endswith("/reject"):
            parts = path.strip("/").split("/")
            event_id, action = parts[-2], parts[-1]
            current = self.store.get(event_id)
            if not current:
                self._json({"error": "Event not found"}, HTTPStatus.NOT_FOUND)
                return
            status = "approved" if action == "approve" else "rejected"
            note = body.get("note") or ("Human approved the proposed action." if action == "approve" else "Human rejected the proposed action.")
            current["decision"]["trace"][4].update({"state": "complete", "note": note, "at": utc_now()})
            current["decision"]["trace"][5].update({"state": "complete", "note": "Action dispatched" if action == "approve" else "Action withheld", "at": utc_now()})
            updated = self.store.update(event_id, status=status, decision=current["decision"], outcome={"state": status, "note": note}, updated_at=utc_now())
            self._json(updated)
            return
        if path.endswith("/feedback"):
            event_id = path.strip("/").split("/")[-2]
            if body.get("rating") not in {"useful", "noise"}:
                self._json({"error": "rating must be useful or noise"}, HTTPStatus.BAD_REQUEST)
                return
            current = self.store.get(event_id)
            if not current:
                self._json({"error": "Event not found"}, HTTPStatus.NOT_FOUND)
                return
            current["decision"]["trace"][6].update({"state": "complete", "note": body["rating"], "at": utc_now()})
            updated = self.store.update(event_id, feedback={"rating": body["rating"], "note": body.get("note", "")}, decision=current["decision"], updated_at=utc_now())
            self._json(updated)
            return
        self._json({"error": "Route not found"}, HTTPStatus.NOT_FOUND)


def seed(store: Store, engine: Engine) -> None:
    if store.list():
        return
    for event in SEED_EVENTS:
        engine.process(event)


def make_server(host: str, port: int, db_path: Path = DEFAULT_DB) -> ThreadingHTTPServer:
    store = Store(db_path)
    engine = Engine(store)
    seed(store, engine)
    handler = type("ConfiguredSignalRoomHandler", (SignalRoomHandler,), {"store": store, "engine": engine})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SignalRoom")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    print(f"SignalRoom listening on http://{args.host}:{args.port}")
    print(f"Run mode: {server.RequestHandlerClass.store.setting('run_mode', 'shadow')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
