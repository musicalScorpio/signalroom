import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend.api.server import SignalRoomHandler, record_worker_recovery
from backend.legacy.core import Engine, Store
from backend.storage.case_store import CaseStore
from backend.storage.reliability_store import ReliabilityStore


class FakeTemporal:
    def __init__(self, store=None, reliability=None):
        self.store = store
        self.reliability = reliability
        self.started = []

    def start_incident(self, payload: dict) -> bool:
        self.started.append(payload)
        if self.store:
            self.store.patch(payload["case_id"], {"status": "investigating"})
        return True

    def query_incident(self, case_id: str) -> dict:
        return {
            "case_id": case_id,
            "status": "awaiting_merge_approval",
            "merge_decision": None,
            "context_checkpoint": {
                "completed_agents": ["investigator", "change_intelligence", "repair", "review"],
                "reviewed_commit": "abc123",
                "approval_state": "pending",
            },
        }

    def query_agent(self, case_id: str, agent_id: str) -> dict:
        return {
            "agent_id": agent_id,
            "agent_name": "Repair Agent",
            "status": "reasoning",
            "turn": 2,
            "assignment_context": {"original_failure": {"error": "KeyError"}, "repository": "/tmp/repo"},
            "observations": [{"tool": "code.inspect", "result": {"editable_files": ["order_service.py"]}}],
        }

    def start_reliability_run(self, payload: dict) -> bool:
        self.started.append(payload)
        if self.reliability:
            self.reliability.patch(payload["run_id"], {"status": "scheduled"})
        return True

    def query_reliability_run(self, run_id: str) -> dict:
        return {"run_id": run_id, "status": "validating", "strategy": "validation"}


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = CaseStore(Path(self.tempdir.name) / "cases.db")
        self.store.create({
            "id": "case_recovery", "title": "Recovery", "status": "awaiting_merge_approval",
            "workflow_id": "signalroom-case-case_recovery",
            "context": {"event": {"type": "checkout.contract_failure"}},
            "approval": {"state": "pending"},
            "evidence": {"commit": {"branch": "fix/recovery", "commit": "abc123"}},
            "agents": {
                "investigator": {"status": "completed"},
                "change_intelligence": {"status": "completed"},
                "repair": {"status": "completed"},
                "review": {"status": "completed"},
                "verification": {"status": "queued"},
            },
            "timeline": [],
        })

    def tearDown(self):
        self.tempdir.cleanup()

    def test_http_case_creation_records_signalroom_to_temporal_handoff(self):
        legacy_store = Store(Path(self.tempdir.name) / "api.db")
        case_store = CaseStore(Path(self.tempdir.name) / "api.db")
        temporal = FakeTemporal(case_store)
        handler = type(
            "IntegrationSignalRoomHandler",
            (SignalRoomHandler,),
            {
                "store": legacy_store,
                "cases": case_store,
                "reliability": ReliabilityStore(Path(self.tempdir.name) / "api.db"),
                "engine": Engine(legacy_store),
                "temporal": temporal,
                "temporal_connected": True,
            },
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/lab/run",
            data=json.dumps({"step_delay": 0.01}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
            payload = json.load(response)

        case_id = payload["case"]["id"]
        self.assertEqual(temporal.started[0]["case_id"], case_id)
        self.assertEqual(temporal.started[0]["step_delay"], 0.01)
        self.assertEqual(temporal.started[0]["side_effect_strategy"], "none")
        self.assertFalse(temporal.started[0]["auto_crash"])
        timeline = case_store.get(case_id)["timeline"]
        self.assertEqual(
            [(item["kind"], item["title"]) for item in timeline],
            [
                ("signalroom", "SignalRoom created the case projection"),
                ("temporal", "Temporal accepted the Workflow start"),
            ],
        )
        self.assertEqual(timeline[1]["data"]["transport"], "Temporal gRPC client")

    @patch("backend.api.server.provider_health", return_value={"status": "ok", "demo_only": True})
    def test_main_repair_case_records_selected_side_effect_policy(self, _provider_health):
        legacy_store = Store(Path(self.tempdir.name) / "unified.db")
        case_store = CaseStore(Path(self.tempdir.name) / "unified.db")
        temporal = FakeTemporal(case_store)
        handler = type(
            "UnifiedCaseHandler", (SignalRoomHandler,),
            {"store": legacy_store, "cases": case_store, "reliability": ReliabilityStore(Path(self.tempdir.name) / "unified.db"),
             "engine": Engine(legacy_store), "temporal": temporal, "temporal_connected": True},
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/lab/run",
            data=json.dumps({"side_effect_strategy": "validation", "auto_crash": True, "step_delay": 0.01}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        case = payload["case"]
        self.assertEqual(case["side_effect"]["strategy"], "validation")
        self.assertEqual(case["side_effect"]["auto_crash_state"], "armed")
        self.assertTrue(case["side_effect"]["payment_code"].startswith("CASE-VALIDATE-"))
        self.assertEqual(temporal.started[0]["operation_id"], f"signalroom:{case['id']}:checkout-payment")

    @patch("backend.api.server.provider_health", return_value={"status": "ok", "demo_only": True})
    def test_http_starts_selected_reliability_strategy(self, _provider_health):
        db_path = Path(self.tempdir.name) / "reliability-api.db"
        legacy_store = Store(db_path)
        case_store = CaseStore(db_path)
        reliability = ReliabilityStore(db_path)
        temporal = FakeTemporal(case_store, reliability)
        handler = type(
            "ReliabilitySignalRoomHandler",
            (SignalRoomHandler,),
            {
                "store": legacy_store,
                "cases": case_store,
                "reliability": reliability,
                "engine": Engine(legacy_store),
                "temporal": temporal,
                "temporal_connected": True,
            },
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/reliability/run",
            data=json.dumps({"strategy": "validation", "hold_seconds": 0.01}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 201)
            payload = json.load(response)

        run = payload["run"]
        self.assertEqual(run["strategy"], "validation")
        self.assertEqual(run["status"], "scheduled")
        self.assertEqual(run["amount_cents"], 6900)
        self.assertTrue(run["payment_code"].startswith("DEMO-VALIDATE-"))
        started = temporal.started[0]
        self.assertEqual(started["run_id"], run["id"])
        self.assertEqual(started["operation_id"], f"signalroom:{run['id']}:create-payment")
        self.assertEqual(started["amount_cents"], 6900)
        self.assertEqual(set(run["agents"]), {"action_agent", "recovery_agent"})
        self.assertIn("payments.create_idempotent", run["agents"]["action_agent"]["tools"])
        self.assertIn("payments.lookup", run["agents"]["recovery_agent"]["tools"])
        self.assertEqual(
            [(item["kind"], item["title"]) for item in run["timeline"]],
            [
                ("agent", "Payment Action Agent received a stable operation identity"),
                ("temporal", "Temporal accepted the reliability Workflow"),
            ],
        )

    @patch("backend.api.server.read_worker_state")
    def test_recovery_observer_records_worker_temporal_and_signalroom_boundaries(self, read_state):
        read_state.return_value = {
            "status": "online", "worker_pid": 222, "restart_count": 1,
            "last_exit_at": "2026-01-01T00:00:01+00:00",
            "last_restart_at": "2026-01-01T00:00:04+00:00",
        }

        interrupted = [{"agent_id": "repair", "name": "Repair Agent", "status": "reasoning", "turn": 2}]
        record_worker_recovery(self.store, FakeTemporal(), "case_recovery", killed_worker_pid=111, interrupted_agents=interrupted)

        timeline = self.store.get("case_recovery")["timeline"]
        self.assertEqual(
            [(item["kind"], item["title"]) for item in timeline],
            [
                ("worker", "Replacement Worker connected"),
                ("temporal", "Temporal replay restored Workflow context"),
                ("signalroom", "SignalRoom reattached to the recovered case"),
            ],
        )
        replay = timeline[1]["data"]
        self.assertEqual(replay["recovered_workflow_state"]["status"], "awaiting_merge_approval")
        self.assertEqual(replay["replayed_context"]["reviewed_commit"], "abc123")
        self.assertEqual(replay["signalroom_projection"]["checkpoint_status"], "awaiting_merge_approval")
        self.assertEqual(timeline[0]["data"]["replacement_worker_pid"], 222)
        self.assertEqual(timeline[0]["data"]["interrupted_agents"], interrupted)
        self.assertEqual(timeline[1]["data"]["recovered_agent_states"]["repair"]["turn"], 2)
        self.assertEqual(timeline[1]["data"]["recovered_agent_states"]["repair"]["observations"][0]["tool"], "code.inspect")

        record_worker_recovery(self.store, FakeTemporal(), "case_recovery", killed_worker_pid=111, interrupted_agents=interrupted)
        self.assertEqual(len(self.store.get("case_recovery")["timeline"]), 3)

        record_worker_recovery(
            self.store,
            FakeTemporal(),
            "case_recovery",
            killed_worker_pid=111,
            interrupted_agents=interrupted,
            recovery_id="2-distinct-trace",
            recovery_cycle=2,
        )
        timeline = self.store.get("case_recovery")["timeline"]
        self.assertEqual(len(timeline), 6)
        self.assertEqual(timeline[3]["id"], "worker-replaced-2-distinct-trace")
        self.assertEqual(timeline[3]["data"]["recovery_cycle"], 2)


if __name__ == "__main__":
    unittest.main()
