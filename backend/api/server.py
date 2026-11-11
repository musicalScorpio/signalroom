#!/usr/bin/env python3
"""SignalRoom HTTP API and static application server."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
import uuid
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.agents.harness import load_definitions, ollama_status
from backend.storage.case_store import CaseStore
from backend.legacy.core import DEFAULT_DB, OUTCOME_CONTRACTS, WEB_ROOT, Engine, Store, seed
from backend.runtime.state import INSTANCE_ID, PROJECT_ROOT, read_worker_state, safe_worker_pid

try:
    from backend.orchestration.client import TemporalBridge
except ImportError:
    TemporalBridge = None  # type: ignore


def wait_for_projection(store: Store, event_id: str, statuses: set[str] | None = None, timeout: float = 1.5) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    record = store.get(event_id)
    while time.monotonic() < deadline:
        record = store.get(event_id)
        if record and (not statuses or record["status"] in statuses):
            return record
        time.sleep(0.05)
    return record


def recovery_context_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    """Operator-safe view of the state Temporal must reconstruct on replay."""
    completed_agents = [
        agent_id for agent_id, agent in record.get("agents", {}).items()
        if agent.get("status") == "completed"
    ]
    return {
        "case_id": record.get("id"),
        "workflow_id": record.get("workflow_id"),
        "checkpoint_status": record.get("status"),
        "signalroom_context": record.get("context", {}),
        "completed_agents": completed_agents,
        "repair_branch": record.get("branch") or record.get("evidence", {}).get("commit", {}).get("branch"),
        "reviewed_commit": record.get("commit") or record.get("evidence", {}).get("commit", {}).get("commit"),
        "approval": record.get("approval", {}),
        "evidence_available": sorted(record.get("evidence", {}).keys()),
    }


def record_worker_recovery(cases: CaseStore, temporal: Any, case_id: str, killed_worker_pid: int) -> None:
    """Observe the supervisor replacement and record Temporal's recovered state."""
    deadline = time.monotonic() + 20
    replacement: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        worker_state = read_worker_state()
        worker_pid = safe_worker_pid(worker_state)
        if worker_state.get("status") == "online" and worker_pid and worker_pid != killed_worker_pid:
            replacement = worker_state
            break
        time.sleep(0.15)

    if not replacement:
        cases.timeline(
            case_id, "worker", "Replacement Worker did not become ready",
            "The supervisor did not publish an online replacement within 20 seconds.",
            {"killed_worker_pid": killed_worker_pid}, status="interrupted",
            event_id=f"worker-recovery-timeout-{killed_worker_pid}",
        )
        return

    replacement_pid = replacement.get("worker_pid")
    cases.timeline(
        case_id, "worker", "Replacement Worker connected",
        f"Supervisor started Worker PID {replacement_pid}; it resumed polling the Temporal task queue.",
        {
            "killed_worker_pid": killed_worker_pid,
            "replacement_worker_pid": replacement_pid,
            "restart_count": replacement.get("restart_count"),
            "last_exit_at": replacement.get("last_exit_at"),
            "last_restart_at": replacement.get("last_restart_at"),
        }, status="running", event_id=f"worker-replaced-{killed_worker_pid}",
        at=replacement.get("last_restart_at"),
    )

    record = cases.get(case_id) or {}
    projection_snapshot = recovery_context_snapshot(record)
    try:
        workflow_state = temporal.query_incident(case_id)
        checkpoint = workflow_state.get("status", record.get("status", "unknown"))
        replay_context = workflow_state.get("context_checkpoint") or projection_snapshot
        cases.timeline(
            case_id, "temporal", "Temporal replay restored Workflow context",
            f"The replacement Worker reconstructed the Workflow from Event History and resumed at {checkpoint}; completed Activities were not rerun.",
            {
                "workflow_id": record.get("workflow_id"),
                "recovered_workflow_state": workflow_state,
                "replayed_context": replay_context,
                "signalroom_projection": projection_snapshot,
            }, status="recovered", event_id=f"temporal-replayed-{killed_worker_pid}",
        )
        cases.timeline(
            case_id, "signalroom", "SignalRoom reattached to the recovered case",
            f"The UI projection still points to the same case and durable checkpoint: {checkpoint}.",
            {"case_id": case_id, "checkpoint_status": checkpoint, "replacement_worker_pid": replacement_pid},
            status="recovered", event_id=f"signalroom-reattached-{killed_worker_pid}",
        )
    except Exception as error:
        cases.timeline(
            case_id, "temporal", "Temporal replay query pending",
            "The Worker was replaced, but its reconstructed Workflow state could not yet be queried.",
            {"workflow_id": record.get("workflow_id"), "error": str(error), "signalroom_projection": projection_snapshot},
            status="interrupted", event_id=f"temporal-query-pending-{killed_worker_pid}",
        )


class SignalRoomHandler(SimpleHTTPRequestHandler):
    store: Store
    engine: Engine
    temporal: Any
    temporal_connected: bool
    cases: CaseStore

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def end_headers(self) -> None:
        # The control room and its JavaScript must always be from the same build.
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    @staticmethod
    def _is_temporal_event(record: dict[str, Any]) -> bool:
        return record.get("decision", {}).get("execution_engine") == "temporal"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/events":
            self._json({"events": self.store.list(), "mode": self.store.setting("run_mode", "shadow"), "runtime": "temporal" if self.temporal_connected else "local-fallback"})
            return
        if path == "/api/metrics":
            self._json(self.store.metrics())
            return
        if path == "/api/cases":
            self._json({"cases": self.cases.list()})
            return
        if path == "/api/agent-definitions":
            self._json({"agents": load_definitions()})
            return
        if path.startswith("/api/cases/"):
            case = self.cases.get(path.rsplit("/", 1)[-1])
            self._json(case if case else {"error": "Case not found"}, HTTPStatus.OK if case else HTTPStatus.NOT_FOUND)
            return
        if path == "/api/runtime":
            self._json({
                "instance_id": INSTANCE_ID,
                "project_root": str(PROJECT_ROOT),
                "server_pid": os.getpid(),
                "temporal_connected": self.temporal_connected,
                "runtime": "temporal" if self.temporal_connected else "local-fallback",
                "temporal_ui": "http://127.0.0.1:8233" if self.temporal_connected else None,
                "worker": read_worker_state(),
                "chaos_demo_enabled": os.getenv("SIGNALROOM_ENABLE_CHAOS_DEMO", "0").lower() in {"1", "true", "on"},
                "ollama": ollama_status(),
                "explanation": "Temporal preserves Workflow history. The local supervisor restarts the Worker process, which replays that history and resumes.",
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
            body.setdefault("id", f"evt_{uuid.uuid4().hex[:8]}")
            body.setdefault("source", "manual.input")
            body.setdefault("summary", "An event arrived without a summary")
            body.setdefault("payload", {})
            body["run_mode"] = self.store.setting("run_mode", "shadow")
            body["outcome_contract"] = OUTCOME_CONTRACTS.get(body.get("type"))
            if self.temporal_connected and body["outcome_contract"]:
                existing = self.store.get(body["id"])
                if existing:
                    self._json({"event": existing, "duplicate": True, "runtime": "temporal"})
                    return
                self.store.upsert(self.engine.queued_record(body, "temporal"))
                created = self.temporal.start_event(body)
                event = wait_for_projection(self.store, body["id"], {"validating", "awaiting_approval", "observing_outcome", "completed"})
                self._json({"event": event, "duplicate": not created, "runtime": "temporal"}, HTTPStatus.CREATED if created else HTTPStatus.OK)
            else:
                event, duplicate = self.engine.process(body)
                self._json({"event": event, "duplicate": duplicate, "runtime": "local-fallback"}, HTTPStatus.OK if duplicate else HTTPStatus.CREATED)
            return

        if path == "/api/lab/run":
            if not self.temporal_connected:
                self._json({"error": "The agent repair lab requires Temporal."}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            case_id = f"case_{uuid.uuid4().hex[:8]}"
            definitions = load_definitions()
            record = {
                "id": case_id,
                "title": "Inventory contract drift breaks checkout",
                "status": "queued",
                "event": {"type": "checkout.contract_failure", "source": "local-order-service", "summary": "Checkout request fails after Inventory Service moved to contract v2"},
                "context": {}, "timeline": [], "evidence": {}, "branch": None, "commit": None,
                "approval": {"state": "not_ready"}, "merge": None, "outcome": None,
                "workflow_id": f"signalroom-case-{case_id}",
                "agents": {agent_id: {"id": agent_id, "name": definition["name"], "purpose": definition["purpose"], "status": "queued", "turn": 0, "observations": [], "accent": definition.get("accent", "acid")} for agent_id, definition in definitions.items()},
            }
            self.cases.create(record)
            self.cases.timeline(
                case_id, "signalroom", "SignalRoom created the case projection",
                "The incoming failure was normalized into a durable case before orchestration began.",
                {"case_id": case_id, "event": record["event"], "workflow_id": record["workflow_id"]},
                event_id=f"signalroom-created-{case_id}",
            )
            created = self.temporal.start_incident({"case_id": case_id, "step_delay": float(body.get("step_delay", 0.35))})
            if not created:
                self._json({"error": "Case Workflow already exists"}, HTTPStatus.CONFLICT)
                return
            self.cases.timeline(
                case_id, "temporal", "Temporal accepted the Workflow start",
                "SignalRoom used the Temporal client; Temporal recorded the start and scheduled work on signalroom-events.",
                {"workflow_id": record["workflow_id"], "task_queue": "signalroom-events", "transport": "Temporal gRPC client"},
                event_id=f"temporal-started-{case_id}",
            )
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                updated = self.cases.get(case_id)
                if updated and updated["status"] != "queued":
                    break
                time.sleep(0.05)
            self._json({"case": self.cases.get(case_id), "runtime": "temporal"}, HTTPStatus.CREATED)
            return

        if path.endswith("/approve-merge") or path.endswith("/reject-merge"):
            parts = path.strip("/").split("/")
            case_id = parts[-2]
            case = self.cases.get(case_id)
            if not case:
                self._json({"error": "Case not found"}, HTTPStatus.NOT_FOUND)
                return
            if case["status"] != "awaiting_merge_approval":
                self._json({"error": "Case is not waiting for merge approval"}, HTTPStatus.CONFLICT)
                return
            approved = path.endswith("/approve-merge")
            decision = {"approved": approved, "note": body.get("note", ""), "commit": case.get("evidence", {}).get("commit", {}).get("commit")}
            self.cases.timeline(
                case_id, "signalroom", "SignalRoom sent the human decision",
                "The API translated the operator action into a Temporal Workflow Signal.",
                decision, status="running", event_id=f"signalroom-decision-{case_id}",
            )
            self.temporal.decide_merge(case_id, approved, decision["note"])
            self.cases.timeline(
                case_id, "temporal", "Temporal accepted the merge-decision Signal",
                "The Signal was appended to Workflow Event History and made the durable wait runnable.",
                {"workflow_id": case.get("workflow_id"), "signal": "merge_decision", "decision": decision},
                event_id=f"temporal-decision-{case_id}",
            )
            self._json({"accepted": True, "approved": approved, "case_id": case_id}, HTTPStatus.ACCEPTED)
            return

        if path == "/api/demo/durability":
            if not self.temporal_connected:
                self._json({"error": "The durability demo requires the Temporal runtime."}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            demo = {
                "id": f"evt_durable_{uuid.uuid4().hex[:8]}",
                "type": "workflow.stalled",
                "source": "durability.demo",
                "summary": "Durability proof: outcome arrives while the agent Worker is offline",
                "payload": {"demo": "worker-crash-resume"},
                "run_mode": "shadow",
                "outcome_contract": OUTCOME_CONTRACTS["workflow.stalled"],
            }
            self.store.upsert(self.engine.queued_record(demo, "temporal"))
            created = self.temporal.start_event(demo)
            event = wait_for_projection(self.store, demo["id"], {"observing_outcome"}, timeout=4.0)
            self._json({"event": event, "created": created, "runtime": "temporal"}, HTTPStatus.CREATED)
            return

        if path == "/api/demo/worker/crash":
            enabled = os.getenv("SIGNALROOM_ENABLE_CHAOS_DEMO", "0").lower() in {"1", "true", "on"}
            if not enabled:
                self._json({"error": "The local crash demo is disabled."}, HTTPStatus.FORBIDDEN)
                return
            worker_state = read_worker_state()
            worker_pid = safe_worker_pid(worker_state, os.getpid())
            if not worker_pid:
                self._json({"error": "No supervised Worker is currently online."}, HTTPStatus.CONFLICT)
                return
            try:
                os.kill(worker_pid, 0)
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                self._json({"error": "The Worker exited before the crash signal arrived."}, HTTPStatus.CONFLICT)
                return
            case_id = body.get("case_id")
            case = self.cases.get(case_id) if case_id else None
            if case_id and case:
                snapshot = recovery_context_snapshot(case)
                self.cases.timeline(
                    case_id, "worker", "Agent Worker killed",
                    f"Worker PID {worker_pid} was terminated. It no longer held live in-process Workflow state.",
                    {"worker_pid": worker_pid, "checkpoint_status": case.get("status")},
                    status="interrupted", event_id=f"worker-killed-{worker_pid}",
                )
                self.cases.timeline(
                    case_id, "temporal", "Temporal retained the durable checkpoint",
                    f"Workflow history remained authoritative at {case.get('status')}; completed agent and tool results stayed recorded.",
                    {
                        "workflow_id": case.get("workflow_id"),
                        "history_owner": "Temporal Event History",
                        "context_to_reconstruct": snapshot,
                    }, status="durable", event_id=f"temporal-retained-{worker_pid}",
                )
                threading.Thread(
                    target=record_worker_recovery,
                    args=(self.cases, self.temporal, case_id, worker_pid),
                    daemon=True,
                    name=f"signalroom-recovery-{case_id}",
                ).start()
            self._json({
                "accepted": True,
                "killed_worker_pid": worker_pid,
                "message": "Worker killed. Temporal still owns the Workflow; the supervisor will start a replacement.",
            }, HTTPStatus.ACCEPTED)
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
            record = self.store.get(event_id)
            if not record:
                self._json({"error": "Event not found"}, HTTPStatus.NOT_FOUND)
                return
            approved = action == "approve"
            note = body.get("note", "")
            if self.temporal_connected and self._is_temporal_event(record):
                self.temporal.approve(event_id, approved, note)
                updated = wait_for_projection(self.store, event_id, {"observing_outcome", "completed"})
            else:
                updated = self.engine.approve(event_id, approved, note)
            self._json(updated)
            return

        if path.endswith("/outcome"):
            event_id = path.strip("/").split("/")[-2]
            record = self.store.get(event_id)
            if not record:
                self._json({"error": "Event not found"}, HTTPStatus.NOT_FOUND)
                return
            contract = record.get("outcome", {}).get("contract")
            valid_codes = {choice["code"] for choice in (contract or {}).get("choices", [])}
            if body.get("code") not in valid_codes:
                self._json({"error": "Outcome code is not valid for this signal", "valid_codes": sorted(valid_codes)}, HTTPStatus.BAD_REQUEST)
                return
            if self.temporal_connected and self._is_temporal_event(record):
                self.temporal.report_outcome(event_id, body)
                updated = wait_for_projection(self.store, event_id, {"completed"}, timeout=2.0)
            else:
                try:
                    updated = self.engine.report_outcome(event_id, body)
                except ValueError as error:
                    self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
            self._json(updated)
            return

        if path.endswith("/feedback"):
            event_id = path.strip("/").split("/")[-2]
            if body.get("rating") not in {"useful", "noise"}:
                self._json({"error": "rating must be useful or noise"}, HTTPStatus.BAD_REQUEST)
                return
            updated = self.engine.feedback(event_id, body["rating"], body.get("note", ""))
            self._json(updated if updated else {"error": "Event not found"}, HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND)
            return

        self._json({"error": "Route not found"}, HTTPStatus.NOT_FOUND)


def make_server(host: str, port: int, db_path: Path = DEFAULT_DB) -> ThreadingHTTPServer:
    store = Store(db_path)
    cases = CaseStore(db_path)
    engine = Engine(store)
    seed(store, engine)
    temporal = TemporalBridge() if TemporalBridge else None
    temporal_connected = bool(temporal and temporal.available())
    handler = type("ConfiguredSignalRoomHandler", (SignalRoomHandler,), {"store": store, "cases": cases, "engine": engine, "temporal": temporal, "temporal_connected": temporal_connected})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SignalRoom")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("SIGNALROOM_PORT", "4173")))
    parser.add_argument("--db", type=Path, default=Path(os.getenv("SIGNALROOM_DB", str(DEFAULT_DB))))
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.db)
    runtime = "Temporal durable workflows" if server.RequestHandlerClass.temporal_connected else "local fallback"
    print(f"SignalRoom listening on http://{args.host}:{args.port}")
    print(f"Runtime: {runtime}")
    print(f"Run mode: {server.RequestHandlerClass.store.setting('run_mode', 'shadow')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


__all__ = ["Store", "Engine", "make_server"]
