import os
import tempfile
import unittest
from pathlib import Path

from server import Engine, Store


class SignalRoomTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "test.db")
        self.engine = Engine(self.store)
        os.environ["SIGNALROOM_MODEL"] = "rules"

    def tearDown(self):
        self.tempdir.cleanup()

    def event(self, event_id="evt_test", event_type="deploy.failed"):
        return {
            "id": event_id,
            "type": event_type,
            "source": "test.system",
            "summary": "A representative test event",
            "payload": {"test": True},
        }

    def test_shadow_mode_never_dispatches_side_effect(self):
        event, duplicate = self.engine.process(self.event())
        self.assertFalse(duplicate)
        self.assertEqual(event["status"], "shadowed")
        self.assertEqual(event["outcome"]["state"], "observed_only")

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
        self.assertEqual(event["status"], "needs_approval")
        self.assertTrue(event["policy"]["requires_human"])

    def test_autonomous_mode_dispatches_reversible_medium_risk(self):
        self.store.set_setting("run_mode", "autonomous")
        event, _ = self.engine.process(self.event(event_type="workflow.stalled"))
        self.assertEqual(event["status"], "acted")
        self.assertFalse(event["policy"]["requires_human"])

    def test_irreversible_action_always_requires_approval(self):
        self.store.set_setting("run_mode", "autonomous")
        event, _ = self.engine.process(self.event(event_type="threshold.breached"))
        self.assertEqual(event["status"], "needs_approval")
        self.assertFalse(event["decision"]["reversible"])


if __name__ == "__main__":
    unittest.main()
