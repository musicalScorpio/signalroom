"""Domain model, durable projection store, and local fallback for SignalRoom."""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.ollama_runtime import selected_ollama_model


ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = ROOT / "frontend"
DEFAULT_DB = ROOT / ".signalroom" / "data" / "signalroom.db"


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


OUTCOME_CONTRACTS: dict[str, dict[str, Any]] = {
    "workflow.stalled": {
        "question": "Did the workflow recover before its SLA was breached?",
        "metric": "recovery_time_minutes",
        "metric_label": "Minutes to recovery",
        "choices": [
            {"code": "workflow_resumed", "label": "Workflow resumed", "success": True},
            {"code": "sla_breached", "label": "SLA breached", "success": False},
        ],
    },
    "threshold.breached": {
        "question": "Was the asset stabilized without a confirmed incident?",
        "metric": "minutes_outside_threshold",
        "metric_label": "Minutes outside threshold",
        "choices": [
            {"code": "asset_stabilized", "label": "Asset stabilized", "success": True},
            {"code": "incident_confirmed", "label": "Incident confirmed", "success": False},
        ],
    },
    "document.uploaded": {
        "question": "What did the compliance review conclude?",
        "metric": "review_time_minutes",
        "metric_label": "Minutes to review",
        "choices": [
            {"code": "cleared_review", "label": "Cleared review", "success": True},
            {"code": "policy_violation", "label": "Policy violation found", "success": False},
        ],
    },
    "support.sla_risk": {
        "question": "Was the customer answered before the SLA deadline?",
        "metric": "minutes_before_deadline",
        "metric_label": "Minutes before deadline",
        "choices": [
            {"code": "sla_avoided", "label": "SLA avoided", "success": True},
            {"code": "sla_breached", "label": "SLA breached", "success": False},
        ],
    },
    "deploy.failed": {
        "question": "Did the service recover after the deployment failure?",
        "metric": "recovery_time_minutes",
        "metric_label": "Minutes to recovery",
        "choices": [
            {"code": "service_recovered", "label": "Service recovered", "success": True},
            {"code": "recovery_failed", "label": "Recovery failed", "success": False},
        ],
    },
}


KEYWORDS = {
    "workflow.stalled": ("stalled", "reconciliation", "workflow", "not advanced", "close process"),
    "threshold.breached": ("threshold", "temperature", "sensor", "breached", "pressure"),
    "document.uploaded": ("document", "contract", "agreement", "uploaded", "pdf"),
    "support.sla_risk": ("ticket", "customer", "support", "response", "sla"),
    "deploy.failed": ("deploy", "build", "rollback", "release", "commit"),
}


SEED_EVENTS = [
    {"id": "evt_7f31", "type": "deploy.failed", "source": "buildkite.prod", "summary": "Checkout API deployment failed after schema migration", "payload": {"service": "checkout-api", "commit": "7e19b2c"}, "received_at": "2026-11-09T14:42:17+00:00"},
    {"id": "evt_441a", "type": "support.sla_risk", "source": "support.queue", "summary": "Enterprise support ticket is 23 minutes from SLA breach", "payload": {"ticket": "T-1842", "minutes_remaining": 23}, "received_at": "2026-11-09T14:38:03+00:00"},
    {"id": "evt_b902", "type": "document.uploaded", "source": "contracts.inbox", "summary": "New vendor agreement document contains regulated data fields", "payload": {"document": "vendor-agreement.pdf", "region": "EU"}, "received_at": "2026-11-09T14:31:49+00:00"},
    {"id": "evt_25c8", "type": "workflow.stalled", "source": "finance.close", "summary": "Month-end reconciliation workflow has stalled for 74 minutes", "payload": {"workflow": "nov-close", "stalled_minutes": 74}, "received_at": "2026-11-09T14:24:11+00:00"},
]


def validate_signal(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("type", "")
    summary = event.get("summary", "").lower()
    if event_type not in RULES:
        return {"valid": False, "issue": f"Unsupported event type: {event_type or 'missing'}", "suggested_type": None}
    scores = {kind: sum(1 for word in words if word in summary) for kind, words in KEYWORDS.items()}
    inferred = max(scores, key=scores.get)
    selected_score = scores.get(event_type, 0)
    inferred_score = scores[inferred]
    if inferred != event_type and inferred_score > 0 and (selected_score == 0 or inferred_score >= selected_score + 2):
        return {
            "valid": False,
            "issue": f"Event type '{event_type}' conflicts with its description; the signal looks like '{inferred}'.",
            "suggested_type": inferred,
            "scores": scores,
        }
    return {"valid": True, "issue": None, "suggested_type": event_type, "scores": scores}


def context_for(event_type: str) -> dict[str, Any]:
    return {"snapshot": RULES[event_type]["context"], "freshness": "current", "captured_at": utc_now()}


def policy_for(recommendation: dict[str, Any], run_mode: str) -> dict[str, Any]:
    requires_human = (
        recommendation["risk"] == "high"
        or not recommendation["reversible"]
        or (run_mode == "guarded" and recommendation["risk"] == "medium")
    )
    return {"run_mode": run_mode, "requires_human": requires_human, "risk": recommendation["risk"], "reversible": recommendation["reversible"]}


def make_trace(event: dict[str, Any], recommendation: dict[str, Any] | None, status: str, validation: dict[str, Any]) -> list[dict[str, Any]]:
    at = event.get("received_at") or utc_now()
    valid = validation.get("valid", False)
    if not valid:
        return [
            {"stage": "event", "state": "complete", "at": at, "note": f"Accepted from {event.get('source', 'manual.input')}"},
            {"stage": "context", "state": "blocked", "at": at, "note": validation.get("issue", "Signal rejected")},
            {"stage": "policy", "state": "bypassed", "at": at, "note": "No policy decision"},
            {"stage": "action", "state": "bypassed", "at": at, "note": "No action proposed"},
            {"stage": "human", "state": "bypassed", "at": at, "note": "No approval requested"},
            {"stage": "execution", "state": "bypassed", "at": at, "note": "No side effect"},
            {"stage": "outcome", "state": "complete", "at": at, "note": "Signal rejected before execution"},
            {"stage": "feedback", "state": "waiting", "at": at, "note": "Unrated"},
        ]
    requires_human = status == "awaiting_approval"
    return [
        {"stage": "event", "state": "complete", "at": at, "note": f"Validated from {event.get('source', 'manual.input')}"},
        {"stage": "context", "state": "complete", "at": at, "note": f"Snapshot assembled from {len(RULES[event['type']]['context'])} sources"},
        {"stage": "policy", "state": "complete", "at": at, "note": f"{recommendation['risk'].title()} risk · {'reversible' if recommendation['reversible'] else 'irreversible'}"},
        {"stage": "action", "state": "complete", "at": at, "note": recommendation["label"]},
        {"stage": "human", "state": "waiting" if requires_human else "bypassed", "at": at, "note": "Approval required" if requires_human else "Within delegated authority"},
        {"stage": "execution", "state": "shadow" if status == "observing_outcome" else "waiting", "at": at, "note": "Side effect withheld in shadow mode" if status == "observing_outcome" else "Waiting"},
        {"stage": "outcome", "state": "waiting", "at": at, "note": OUTCOME_CONTRACTS[event["type"]]["question"]},
        {"stage": "feedback", "state": "waiting", "at": at, "note": "Unrated"},
    ]


def update_trace_stage(record: dict[str, Any], stage: str, state: str, note: str) -> None:
    for item in record.get("decision", {}).get("trace", []):
        if item.get("stage") == stage:
            item.update({"state": state, "note": note, "at": utc_now()})
            return


class Store:
    def __init__(self, path: Path = DEFAULT_DB):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event_type TEXT NOT NULL, source TEXT NOT NULL,
                    summary TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, context TEXT NOT NULL, policy TEXT NOT NULL,
                    decision TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT NOT NULL,
                    feedback TEXT NOT NULL, model_mode TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS effects (
                    idempotency_key TEXT PRIMARY KEY, event_id TEXT NOT NULL,
                    action TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
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
            db.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def put(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        existing = self.get(record["id"])
        if existing:
            return existing, True
        self.upsert(record)
        return record, False

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        values = self._values(record)
        with self.connect() as db:
            db.execute(
                """INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                event_type=excluded.event_type, source=excluded.source, summary=excluded.summary,
                payload=excluded.payload, received_at=excluded.received_at, updated_at=excluded.updated_at,
                context=excluded.context, policy=excluded.policy, decision=excluded.decision,
                status=excluded.status, outcome=excluded.outcome, feedback=excluded.feedback,
                model_mode=excluded.model_mode""",
                values,
            )
        return record

    @staticmethod
    def _values(record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            record["id"], record["type"], record["source"], record["summary"], json.dumps(record.get("payload", {})),
            record["received_at"], record.get("updated_at", utc_now()), json.dumps(record.get("context", {})),
            json.dumps(record.get("policy", {})), json.dumps(record.get("decision", {})), record["status"],
            json.dumps(record.get("outcome", {})), json.dumps(record.get("feedback", {})), record.get("model_mode", "transparent-rules"),
        )

    def list(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY received_at DESC").fetchall()
        return [deserialize(row) for row in rows]

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return deserialize(row) if row else None

    def update(self, event_id: str, **changes: Any) -> dict[str, Any] | None:
        record = self.get(event_id)
        if not record:
            return None
        record.update(changes)
        record["updated_at"] = utc_now()
        return self.upsert(record)

    def record_effect(self, idempotency_key: str, event_id: str, action: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT result FROM effects WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row:
                return json.loads(row["result"])
            result = {
                "state": "dispatched", "reference": f"act_{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex[:10]}",
                "idempotency_key": idempotency_key, "dispatched_at": utc_now(), "note": action,
            }
            db.execute("INSERT INTO effects VALUES (?, ?, ?, ?, ?)", (idempotency_key, event_id, action, json.dumps(result), utc_now()))
        return result

    def metrics(self) -> dict[str, int]:
        events = self.list()
        business = [event.get("outcome", {}).get("business_result") for event in events]
        completed = [item for item in business if item and item.get("success") in {True, False}]
        return {
            "total": len(events),
            "completed": len(completed),
            "successful": sum(1 for item in completed if item.get("success") is True),
            "failed": sum(1 for item in completed if item.get("success") is False),
            "pending": sum(1 for event in events if event["status"] in {"queued", "validating", "awaiting_approval", "observing_outcome"}),
            "invalid": sum(1 for event in events if not event.get("validation", {}).get("valid", True)),
            "useful": sum(1 for event in events if event.get("feedback", {}).get("rating") == "useful"),
            "noise": sum(1 for event in events if event.get("feedback", {}).get("rating") == "noise"),
        }


def deserialize(row: sqlite3.Row) -> dict[str, Any]:
    decision = json.loads(row["decision"])
    outcome = json.loads(row["outcome"])
    event_type = row["event_type"]
    if "contract" not in outcome and event_type in OUTCOME_CONTRACTS:
        old_state = outcome.get("state", "pending")
        outcome = {
            "state": "pending" if old_state in {"pending", "observed_only"} else old_state,
            "contract": OUTCOME_CONTRACTS[event_type],
            "action_result": {"state": "withheld_shadow", "note": outcome.get("note", "Legacy event")},
            "business_result": None,
        }
    legacy_statuses = {"shadowed": "observing_outcome", "needs_approval": "awaiting_approval", "acted": "observing_outcome", "approved": "observing_outcome", "rejected": "observing_outcome"}
    return {
        "id": row["id"], "type": event_type, "source": row["source"], "summary": row["summary"],
        "payload": json.loads(row["payload"]), "received_at": row["received_at"], "updated_at": row["updated_at"],
        "validation": decision.get("validation", {"valid": True, "issue": None}),
        "context": json.loads(row["context"]), "policy": json.loads(row["policy"]), "decision": decision,
        "status": legacy_statuses.get(row["status"], row["status"]), "outcome": outcome, "feedback": json.loads(row["feedback"]), "model_mode": row["model_mode"],
    }


class Engine:
    """Local fallback. Temporal is the preferred runtime; this keeps the demo usable offline."""

    def __init__(self, store: Store):
        self.store = store

    def queued_record(self, event: dict[str, Any], engine: str = "temporal") -> dict[str, Any]:
        now = event.get("received_at") or utc_now()
        event_id = event.get("id") or f"evt_{uuid.uuid4().hex[:8]}"
        return {
            "id": event_id, "type": event.get("type", "workflow.stalled"), "source": event.get("source", "manual.input"),
            "summary": event.get("summary", "An event arrived without a summary"), "payload": event.get("payload", {}),
            "received_at": now, "updated_at": utc_now(), "validation": {}, "context": {}, "policy": {},
            "decision": {"execution_engine": engine, "workflow_id": f"signalroom-{event_id}", "trace": []},
            "status": "queued", "outcome": {"state": "pending", "contract": OUTCOME_CONTRACTS.get(event.get("type")), "action_result": None, "business_result": None},
            "feedback": {}, "model_mode": "pending",
        }

    def recommend(self, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
        rule = RULES[event["type"]]
        recommendation = asdict(rule["action"])
        model_decision, model_mode = self._ask_local_model(event, rule)
        if model_decision:
            recommendation.update({key: value for key, value in model_decision.items() if key in recommendation})
            recommendation["confidence"] = float(recommendation["confidence"])
            recommendation["reversible"] = bool(recommendation["reversible"])
        return recommendation, model_mode

    def process(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        record = self.queued_record(event, "local-fallback")
        existing = self.store.get(record["id"])
        if existing:
            return existing, True
        validation = validate_signal(record)
        record["validation"] = validation
        if not validation["valid"]:
            record["status"] = "completed"
            record["decision"].update({"validation": validation, "trace": make_trace(record, None, "completed", validation)})
            record["outcome"] = {"state": "complete", "contract": None, "action_result": {"state": "not_executed"}, "business_result": {"code": "signal_rejected", "label": "Signal rejected", "success": None, "note": validation["issue"], "reported_at": utc_now()}}
            self.store.upsert(record)
            return record, False
        recommendation, model_mode = self.recommend(record)
        run_mode = self.store.setting("run_mode", "shadow")
        policy = policy_for(recommendation, run_mode)
        status = "awaiting_approval" if run_mode != "shadow" and policy["requires_human"] else "observing_outcome"
        action_result = None
        if run_mode == "shadow":
            action_result = {"state": "withheld_shadow", "note": "No side effect in shadow mode."}
        elif not policy["requires_human"]:
            action_result = self.store.record_effect(f"signalroom-{record['id']}:action", record["id"], recommendation["label"])
        record.update({"context": context_for(record["type"]), "policy": policy, "status": status, "model_mode": model_mode})
        record["decision"].update({**recommendation, "validation": validation, "trace": make_trace(record, recommendation, status, validation)})
        record["outcome"] = {"state": "pending", "contract": OUTCOME_CONTRACTS[record["type"]], "action_result": action_result, "business_result": None}
        self.store.upsert(record)
        return record, False

    def approve(self, event_id: str, approved: bool, note: str = "") -> dict[str, Any] | None:
        record = self.store.get(event_id)
        if not record:
            return None
        if approved:
            record["outcome"]["action_result"] = self.store.record_effect(f"signalroom-{event_id}:action", event_id, record["decision"]["label"])
            record["status"] = "observing_outcome"
            update_trace_stage(record, "human", "complete", note or "Action approved")
            update_trace_stage(record, "execution", "complete", "Action dispatched")
        else:
            record["outcome"]["action_result"] = {"state": "rejected", "note": note or "Action rejected"}
            record["status"] = "observing_outcome"
            update_trace_stage(record, "human", "complete", note or "Action rejected")
            update_trace_stage(record, "execution", "bypassed", "No side effect")
        return self.store.upsert(record)

    def report_outcome(self, event_id: str, report: dict[str, Any]) -> dict[str, Any]:
        record = self.store.get(event_id)
        if not record:
            raise KeyError("Event not found")
        contract = record["outcome"].get("contract")
        if not contract:
            raise ValueError("This event has no outcome contract")
        choice = next((item for item in contract["choices"] if item["code"] == report.get("code")), None)
        if not choice:
            raise ValueError("Outcome code is not valid for this signal")
        business_result = {**choice, "note": report.get("note", ""), "metrics": report.get("metrics", {}), "reported_at": utc_now()}
        record["outcome"].update({"state": "complete", "business_result": business_result})
        record["status"] = "completed"
        update_trace_stage(record, "outcome", "complete", choice["label"])
        return self.store.upsert(record)

    def feedback(self, event_id: str, rating: str, note: str = "") -> dict[str, Any] | None:
        record = self.store.get(event_id)
        if not record:
            return None
        record["feedback"] = {"rating": rating, "note": note}
        update_trace_stage(record, "feedback", "complete", rating)
        return self.store.upsert(record)

    @staticmethod
    def _ask_local_model(event: dict[str, Any], rule: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if os.getenv("SIGNALROOM_MODEL", "rules") != "ollama":
            return None, "transparent-rules"
        endpoint = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
        try:
            model = selected_ollama_model()
        except RuntimeError:
            return None, "transparent-rules (no local Ollama model found)"
        prompt = "Return JSON only with label, rationale, risk, confidence, reversible, owner. Prefer no action for weak signals. Event: " + json.dumps(event) + ". Context: " + json.dumps(rule["context"])
        request = urllib.request.Request(endpoint, data=json.dumps({"model": model, "stream": False, "format": "json", "messages": [{"role": "user", "content": prompt}], "options": {"temperature": 0.1}}).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.loads(response.read())
                return json.loads(result.get("message", {}).get("content", "{}")), f"ollama/{model}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            return None, f"transparent-rules (local model unavailable: {model})"


def seed(store: Store, engine: Engine) -> None:
    if store.list():
        return
    for event in SEED_EVENTS:
        engine.process(event)
