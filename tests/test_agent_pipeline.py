import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agents.harness import load_definitions, model_turn
from backend.agents.tools import repair_lab
from backend.api.server import recovery_context_snapshot
from backend.orchestration.workflows import normalize_agent_result
from backend.storage.case_store import CaseStore


MODEL_AUTHORED_REPAIR = '''"""Order service supporting inventory contract versions one and two."""

from inventory_service import get_inventory


def available_quantity(inventory: dict) -> int:
    if "available_quantity" in inventory:
        return int(inventory["available_quantity"])
    stock = inventory.get("stock")
    if isinstance(stock, dict) and "on_hand" in stock and "reserved" in stock:
        return max(0, int(stock["on_hand"]) - int(stock["reserved"]))
    raise ValueError("Unsupported inventory contract")


def can_fulfill(sku: str, quantity: int) -> bool:
    return available_quantity(get_inventory(sku)) >= quantity


def checkout(sku: str, quantity: int) -> dict:
    if not can_fulfill(sku, quantity):
        return {"status": "rejected", "reason": "insufficient_stock"}
    return {"status": "accepted", "sku": sku, "quantity": quantity}
'''


class AgentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_agent_definitions_are_bounded_and_valid(self):
        definitions = load_definitions()
        self.assertEqual(
            set(definitions),
            {"investigator", "change_intelligence", "repair", "review", "verification"},
        )
        for definition in definitions.values():
            self.assertTrue(definition["system_prompt"])
            self.assertGreater(definition["max_turns"], len(definition["required_tools"]))
            self.assertLessEqual(set(definition["required_tools"]), set(definition["tools"]))

    def test_safe_policy_follows_required_tool_contract(self):
        definition = load_definitions()["investigator"]
        with patch.dict(os.environ, {"SIGNALROOM_AGENT_MODEL": "policy"}):
            first = model_turn(definition, {"case_id": "test"}, [])
            self.assertEqual(first["tool"], "tests.reproduce")
            second = model_turn(definition, {"case_id": "test"}, [{"tool": "tests.reproduce", "result": {}}])
            self.assertEqual(second["tool"], "logs.read")
            done = model_turn(
                definition,
                {"case_id": "test"},
                [{"tool": "tests.reproduce", "result": {}}, {"tool": "logs.read", "result": {}}],
            )
            self.assertEqual(done["type"], "complete")

    def test_malformed_open_model_completion_is_replay_safe(self):
        normalized = normalize_agent_result({
            "type": "complete",
            "result": "All tests passed.",
            "model_mode": "ollama/test",
        })
        self.assertEqual(normalized["model_output"], "All tests passed.")
        self.assertEqual(normalized["summary"], "Agent completed")

    def test_repair_agent_cannot_use_deterministic_fallback(self):
        definition = load_definitions()["repair"]
        with patch.dict(os.environ, {"SIGNALROOM_AGENT_MODEL": "policy"}):
            with self.assertRaisesRegex(RuntimeError, "requires Ollama"):
                model_turn(definition, {"case_id": "test"}, [{"tool": "code.inspect", "result": {}}])

    def test_case_store_preserves_projection_and_timeline(self):
        store = CaseStore(self.root / "cases.db")
        store.create({"id": "case_test", "title": "Test", "status": "queued", "agents": {}})
        store.agent("case_test", "repair", status="using_tool", active_tool="tests.run")
        store.timeline("case_test", "tool", "Tests started", data={"command": ["python", "-m", "unittest"]})
        record = store.get("case_test")
        self.assertEqual(record["agents"]["repair"]["active_tool"], "tests.run")
        self.assertEqual(record["timeline"][0]["data"]["command"][-1], "unittest")

    def test_recovery_timeline_is_idempotent_and_exposes_checkpoint_context(self):
        store = CaseStore(self.root / "cases.db")
        store.create({
            "id": "case_recovery", "title": "Recovery", "status": "awaiting_merge_approval",
            "workflow_id": "signalroom-case-case_recovery",
            "context": {"repo": "/tmp/repo", "event": {"type": "contract.failure"}},
            "approval": {"state": "pending"},
            "evidence": {"commit": {"branch": "fix", "commit": "abc123"}},
            "agents": {"repair": {"status": "completed"}, "verification": {"status": "queued"}},
        })
        store.timeline("case_recovery", "temporal", "Checkpoint retained", event_id="temporal-retained-42", at="2026-01-01T00:00:00+00:00")
        store.timeline("case_recovery", "temporal", "Checkpoint retained", event_id="temporal-retained-42", at="2026-01-01T00:00:00+00:00")
        record = store.get("case_recovery")
        self.assertEqual(len(record["timeline"]), 1)
        self.assertEqual(record["timeline"][0]["at"], "2026-01-01T00:00:00+00:00")
        snapshot = recovery_context_snapshot(record)
        self.assertEqual(snapshot["checkpoint_status"], "awaiting_merge_approval")
        self.assertEqual(snapshot["completed_agents"], ["repair"])
        self.assertEqual(snapshot["reviewed_commit"], "abc123")

    def test_real_git_repair_reproduces_fixes_merges_and_verifies(self):
        with patch.object(repair_lab, "LAB_ROOT", self.root / "repair-lab"):
            case_id = "case_integration"
            prepared = repair_lab.prepare_case(case_id)
            self.assertNotEqual(prepared["failure"]["exit_code"], 0)
            inspection = repair_lab.inspect_code(case_id)
            self.assertEqual(inspection["editable_files"], ["order_service.py"])
            repair = repair_lab.apply_repair(case_id, {
                "file": "order_service.py",
                "replacement_content": MODEL_AUTHORED_REPAIR,
                "rationale": "Support both observed inventory contracts.",
            })
            self.assertIn("available_quantity", repair["patch"])
            self.assertTrue(repair["model_authored"])
            self.assertEqual(repair_lab.run_tests(case_id)["exit_code"], 0)
            commit = repair_lab.commit_repair(case_id)
            review = repair_lab.review_diff(case_id)
            self.assertTrue(review["approved"])
            merged = repair_lab.merge(case_id, commit["commit"])
            self.assertTrue(merged["merged"])
            verified = repair_lab.verify_main(case_id)
            self.assertTrue(verified["success"])
            self.assertEqual(verified["checkout_response"]["status"], "accepted")

    def test_model_patch_tool_rejects_unscoped_or_missing_source(self):
        with patch.object(repair_lab, "LAB_ROOT", self.root / "repair-lab"):
            case_id = "case_guardrails"
            repair_lab.prepare_case(case_id)
            with self.assertRaisesRegex(ValueError, "only replace order_service.py"):
                repair_lab.apply_repair(case_id, {"file": "inventory_service.py", "replacement_content": "pass\n"})
            with self.assertRaisesRegex(ValueError, "replacement_content"):
                repair_lab.apply_repair(case_id, {"file": "order_service.py", "replacement_content": ""})


if __name__ == "__main__":
    unittest.main()
