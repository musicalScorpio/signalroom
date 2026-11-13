import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.agents.harness import load_definitions, model_turn, ollama_output_budget, ollama_think_setting, parse_model_json
from backend.ollama_runtime import choose_model
from backend.agents.tools import repair_lab
from backend.api.server import active_agent_tasks, recovery_context_snapshot
from backend.orchestration.activities import TOOL_WAIT_SUMMARIES, model_wait_summary
from backend.orchestration.workflows import LOCAL_ACTIVITY_HEARTBEAT_TIMEOUT, normalize_agent_result
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
            {"investigator", "change_intelligence", "repair", "side_effect", "review", "verification"},
        )
        for definition in definitions.values():
            self.assertTrue(definition["system_prompt"])
            self.assertGreater(definition["max_turns"], len(definition["required_tools"]))
            self.assertLessEqual(set(definition["required_tools"]), set(definition["tools"]))

    def test_ollama_model_selection_prefers_running_model_over_missing_default(self):
        selected, source = choose_model("qwen3:4b", ["gpt-oss:20b"], ["gpt-oss:20b"])
        self.assertEqual(selected, "gpt-oss:20b")
        self.assertEqual(source, "running")

        selected, source = choose_model("", [], ["gpt-oss:20b"])
        self.assertEqual(selected, "gpt-oss:20b")
        self.assertEqual(source, "installed")

    def test_gpt_oss_uses_low_reasoning_to_preserve_json_output_budget(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OLLAMA_THINK", None)
            self.assertEqual(ollama_think_setting("gpt-oss:20b"), "low")
            self.assertFalse(ollama_think_setting("qwen3:4b"))
        with patch.dict(os.environ, {"OLLAMA_THINK": "false"}):
            self.assertFalse(ollama_think_setting("gpt-oss:20b"))

    def test_model_json_parser_accepts_one_object_and_ignores_trailing_chatter(self):
        self.assertEqual(parse_model_json('{"status":"ok"}\nextra explanation'), {"status": "ok"})
        self.assertEqual(parse_model_json('```json\n{"status":"ok"}\n```'), {"status": "ok"})
        with self.assertRaisesRegex(ValueError, "does not contain"):
            parse_model_json("no structured output")

    def test_local_model_output_budgets_keep_turns_short(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OLLAMA_NUM_PREDICT", None)
            self.assertEqual(ollama_output_budget(False), 512)
            self.assertEqual(ollama_output_budget(True), 4096)
        with patch.dict(os.environ, {"OLLAMA_NUM_PREDICT": "900"}):
            self.assertEqual(ollama_output_budget(True), 900)

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

    def test_local_model_wait_state_is_explicit_and_not_a_four_second_deadline(self):
        definition = load_definitions()["repair"]
        summary = model_wait_summary(definition, turn=2, attempt=1)
        self.assertIn("Waiting for the local Ollama model", summary)
        self.assertIn("local inference can take several minutes", summary)
        self.assertEqual(LOCAL_ACTIVITY_HEARTBEAT_TIMEOUT.total_seconds(), 30)
        self.assertIn("complete local regression suite", TOOL_WAIT_SUMMARIES["tests.run"])

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

    def test_worker_kill_snapshot_names_every_active_agent_task(self):
        tasks = active_agent_tasks({
            "agents": {
                "investigator": {"name": "Reproduction Agent", "status": "reasoning", "turn": 2},
                "change_intelligence": {"name": "Change Intelligence Agent", "status": "using_tool", "turn": 2, "active_tool": "git.history"},
                "repair": {"name": "Repair Agent", "status": "queued", "turn": 0},
                "review": {"name": "Review Agent", "status": "completed", "turn": 3},
            }
        })
        self.assertEqual([task["name"] for task in tasks], ["Reproduction Agent", "Change Intelligence Agent"])
        self.assertEqual(tasks[1]["active_tool"], "git.history")

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

    def test_api_strategy_adds_a_real_provider_contract_to_the_model_surface(self):
        with patch.object(repair_lab, "LAB_ROOT", self.root / "repair-lab"):
            case_id = "case_api_contract"
            prepared = repair_lab.prepare_case(case_id, "native_idempotency")
            inspection = repair_lab.inspect_code(case_id)
            self.assertEqual(prepared["side_effect_strategy"], "native_idempotency")
            self.assertIn("create_payment", inspection["payment_gateway.py"])
            self.assertIn("Idempotency-Key", inspection["payment_gateway.py"])
            self.assertIn("assert_called_once", inspection["tests/test_contract.py"])


if __name__ == "__main__":
    unittest.main()
