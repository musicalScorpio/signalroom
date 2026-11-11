import os
import tempfile
import unittest
from pathlib import Path

from backend.legacy.core import Engine, OUTCOME_CONTRACTS, Store
from backend.runtime.state import read_worker_state, safe_worker_pid, write_worker_state


SUMMARIES = {
    "workflow.stalled": "Payment reconciliation workflow has stalled for 61 minutes",
    "threshold.breached": "Warehouse temperature threshold was breached for 9 minutes",
    "document.uploaded": "A vendor agreement document was uploaded for review",
    "support.sla_risk": "Enterprise support ticket is approaching its SLA deadline",
    "deploy.failed": "Checkout API deployment failed after a schema migration",
}


class SignalRoomTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "test.db")
        self.engine = Engine(self.store)
        os.environ["SIGNALROOM_MODEL"] = "rules"

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, event_id="evt_test", event_type="deploy.failed"):
        return {"id": event_id, "type": event_type, "source": "test.system", "summary": SUMMARIES[event_type], "payload": {"test": True}}

    def test_shadow_mode_withholds_action_but_waits_for_business_outcome(self):
        event, duplicate = self.engine.process(self.event())
        self.assertFalse(duplicate)
        self.assertEqual(event["status"], "observing_outcome")
        self.assertEqual(event["outcome"]["action_result"]["state"], "withheld_shadow")
        self.assertIsNone(event["outcome"]["business_result"])

    def test_duplicate_event_is_idempotent(self):
        first, first_duplicate = self.engine.process(self.event())
        second, second_duplicate = self.engine.process(self.event())
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_guarded_mode_escalates_medium_risk(self):
        self.store.set_setting("run_mode", "guarded")
        event, _ = self.engine.process(self.event(event_type="workflow.stalled"))
        self.assertEqual(event["status"], "awaiting_approval")
        self.assertTrue(event["policy"]["requires_human"])

    def test_autonomous_mode_dispatches_reversible_medium_risk_once(self):
        self.store.set_setting("run_mode", "autonomous")
        event, _ = self.engine.process(self.event(event_type="workflow.stalled"))
        self.assertEqual(event["status"], "observing_outcome")
        first_reference = event["outcome"]["action_result"]["reference"]
        same_effect = self.store.record_effect("signalroom-evt_test:action", "evt_test", event["decision"]["label"])
        self.assertEqual(first_reference, same_effect["reference"])

    def test_irreversible_action_always_requires_approval(self):
        self.store.set_setting("run_mode", "autonomous")
        event, _ = self.engine.process(self.event(event_type="threshold.breached"))
        self.assertEqual(event["status"], "awaiting_approval")
        self.assertFalse(event["decision"]["reversible"])

    def test_every_signal_type_has_a_terminal_business_outcome(self):
        for index, event_type in enumerate(OUTCOME_CONTRACTS):
            with self.subTest(event_type=event_type):
                event_id = f"evt_outcome_{index}"
                event, _ = self.engine.process(self.event(event_id, event_type))
                success_choice = event["outcome"]["contract"]["choices"][0]
                completed = self.engine.report_outcome(event_id, {"code": success_choice["code"], "note": "Verified by source system", "metrics": {event["outcome"]["contract"]["metric"]: 12}})
                self.assertEqual(completed["status"], "completed")
                self.assertTrue(completed["outcome"]["business_result"]["success"])

    def test_type_description_conflict_is_rejected_before_agent_action(self):
        event = self.event(event_type="deploy.failed")
        event["summary"] = SUMMARIES["workflow.stalled"]
        result, _ = self.engine.process(event)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["validation"]["valid"])
        self.assertEqual(result["validation"]["suggested_type"], "workflow.stalled")
        self.assertEqual(result["outcome"]["business_result"]["code"], "signal_rejected")

    def test_worker_state_is_written_atomically_and_read_back(self):
        path = Path(self.tempdir.name) / "worker-state.json"
        expected = {"status": "online", "worker_pid": 4321, "restart_count": 2}
        write_worker_state(expected, path)
        self.assertEqual(read_worker_state(path), expected)

    def test_crash_demo_rejects_unsafe_worker_pids(self):
        self.assertIsNone(safe_worker_pid({"worker_pid": True}))
        self.assertIsNone(safe_worker_pid({"worker_pid": 1}))
        self.assertIsNone(safe_worker_pid({"worker_pid": 9000}, server_pid=9000))
        self.assertEqual(safe_worker_pid({"worker_pid": 9001}, server_pid=9000), 9001)


if __name__ == "__main__":
    unittest.main()
