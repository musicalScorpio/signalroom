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


class FakeTemporal:
    def __init__(self, store=None):
        self.store = store
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
        self.assertEqual(temporal.started, [{"case_id": case_id, "step_delay": 0.01}])
        timeline = case_store.get(case_id)["timeline"]
        self.assertEqual(
            [(item["kind"], item["title"]) for item in timeline],
            [
                ("signalroom", "SignalRoom created the case projection"),
                ("temporal", "Temporal accepted the Workflow start"),
            ],
        )
        self.assertEqual(timeline[1]["data"]["transport"], "Temporal gRPC client")

    @patch("backend.api.server.read_worker_state")
    def test_recovery_observer_records_worker_temporal_and_signalroom_boundaries(self, read_state):
        read_state.return_value = {
            "status": "online", "worker_pid": 222, "restart_count": 1,
            "last_exit_at": "2026-01-01T00:00:01+00:00",
            "last_restart_at": "2026-01-01T00:00:04+00:00",
        }

        record_worker_recovery(self.store, FakeTemporal(), "case_recovery", killed_worker_pid=111)

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

        record_worker_recovery(self.store, FakeTemporal(), "case_recovery", killed_worker_pid=111)
        self.assertEqual(len(self.store.get("case_recovery")["timeline"]), 3)


if __name__ == "__main__":
    unittest.main()
