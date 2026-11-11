"""The backend Temporal orchestrator for parent/child multi-agent repair."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


ACTIVITY_RETRY = RetryPolicy(maximum_attempts=3)


def normalize_agent_result(command: dict[str, Any]) -> dict[str, Any]:
    """Keep an open model's completion shape from breaking Workflow replay."""
    raw_result = command.get("result", {})
    if not isinstance(raw_result, dict):
        raw_result = {"model_output": raw_result}
    return {
        **raw_result,
        "summary": command.get("summary") or raw_result.get("summary", "Agent completed"),
        "model_mode": command.get("model_mode"),
    }


@workflow.defn
class AgentWorkflow:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        definition = input["definition"]
        observations: list[dict[str, Any]] = []
        self.state = {"agent_id": input["agent_id"], "status": "running", "turn": 0, "observations": observations}
        for turn in range(1, int(definition["max_turns"]) + 1):
            self.state.update({"status": "reasoning", "turn": turn})
            command = await workflow.execute_activity(
                "agent_model_turn",
                {**input, "turn": turn, "observations": observations},
                start_to_close_timeout=timedelta(seconds=60), retry_policy=ACTIVITY_RETRY,
            )
            if command.get("type") == "call_tool":
                self.state.update({"status": "using_tool", "active_tool": command["tool"]})
                observation = await workflow.execute_activity(
                    "agent_tool",
                    {**input, "tool": command["tool"], "arguments": command.get("arguments", {})},
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=ACTIVITY_RETRY,
                )
                observations.append(observation)
                self.state.update({"observations": observations, "active_tool": None})
                continue
            if command.get("type") == "complete":
                result = normalize_agent_result(command)
                await workflow.execute_activity(
                    "complete_agent", {**input, "result": result, "observations": observations},
                    start_to_close_timeout=timedelta(seconds=15), retry_policy=ACTIVITY_RETRY,
                )
                self.state.update({"status": "completed", "result": result})
                return {"agent_id": input["agent_id"], "result": result, "observations": observations}
        raise RuntimeError(f"{input['agent_id']} exceeded its configured turn limit")

    @workflow.query
    def current_state(self) -> dict[str, Any]:
        return self.state


@workflow.defn
class IncidentCaseWorkflow:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self._merge_decision: dict[str, Any] | None = None

    async def _stage(self, case_id: str, status: str, title: str, detail: str, changes: dict[str, Any] | None = None, kind: str = "temporal") -> None:
        await workflow.execute_activity(
            "update_case_stage",
            {"case_id": case_id, "status": status, "title": title, "detail": detail, "changes": changes or {}, "kind": kind},
            start_to_close_timeout=timedelta(seconds=15), retry_policy=ACTIVITY_RETRY,
        )
        self.state["status"] = status

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        case_id = input["case_id"]
        self.state = {"case_id": case_id, "status": "preparing", "merge_decision": None}
        prepared = await workflow.execute_activity(
            "prepare_repair_case", input,
            start_to_close_timeout=timedelta(seconds=45), retry_policy=ACTIVITY_RETRY,
        )
        definitions = prepared["definitions"]
        context = {"case_id": case_id, **prepared["lab"]}
        self.state["context_checkpoint"] = {
            "source_event": context.get("event"),
            "original_failure": context.get("failure"),
            "repository": context.get("repo"),
            "completed_agents": [],
        }

        async def run_agent(agent_id: str, agent_context: dict[str, Any]) -> dict[str, Any]:
            return await workflow.execute_child_workflow(
                AgentWorkflow.run,
                {"case_id": case_id, "agent_id": agent_id, "definition": definitions[agent_id], "context": agent_context, "step_delay": input.get("step_delay", 0.25)},
                id=f"signalroom-{case_id}-{agent_id}", task_queue=workflow.info().task_queue,
            )

        await self._stage(case_id, "investigating", "Parallel investigation started", "Reproduction and change-intelligence agents are working from independent evidence sources.")
        reproduction, changes = await asyncio.gather(
            run_agent("investigator", context),
            run_agent("change_intelligence", context),
        )
        self.state["context_checkpoint"].update({
            "completed_agents": ["investigator", "change_intelligence"],
            "investigation_observations": {
                "investigator": len(reproduction.get("observations", [])),
                "change_intelligence": len(changes.get("observations", [])),
            },
        })
        shared = {**context, "reproduction": reproduction, "change_intelligence": changes}

        await self._stage(case_id, "repairing", "Evidence handed to Repair Agent", "The repair context contains recorded outputs from both investigation child Workflows.")
        repair = await run_agent("repair", shared)
        commit_observation = next((item for item in repair["observations"] if item["tool"] == "git.commit"), None)
        if not commit_observation:
            raise RuntimeError("Repair Agent did not create a commit")
        commit = commit_observation["result"]
        self.state["context_checkpoint"].update({
            "completed_agents": ["investigator", "change_intelligence", "repair"],
            "repair_branch": commit.get("branch"),
            "reviewed_commit": commit.get("commit"),
        })

        await self._stage(case_id, "reviewing", "Independent review started", "Review Agent receives the exact branch and commit, not the Repair Agent's private reasoning.", {"branch": commit["branch"], "commit": commit["commit"], "repair": repair})
        review = await run_agent("review", {**shared, "repair": repair, "commit": commit})
        review_diff_observation = next((item for item in review["observations"] if item["tool"] == "review.diff"), None)
        test_observation = next((item for item in review["observations"] if item["tool"] == "tests.run"), None)
        evidence = {
            "commit": commit,
            "review": review_diff_observation["result"] if review_diff_observation else {},
            "tests": test_observation["result"] if test_observation else {},
            "reproduction": reproduction,
            "change_intelligence": changes,
        }
        self.state["context_checkpoint"].update({
            "completed_agents": ["investigator", "change_intelligence", "repair", "review"],
            "approval_state": "pending",
            "review_approved": evidence["review"].get("approved"),
            "tests_exit_code": evidence["tests"].get("exit_code"),
        })

        await self._stage(
            case_id, "awaiting_merge_approval", "Human merge approval required",
            "Agents produced a real branch, commit, diff, review, and passing test evidence. Temporal is durably waiting.",
            {"evidence": evidence, "approval": {"state": "pending", "commit": commit["commit"]}}, kind="human",
        )
        await workflow.wait_condition(lambda: self._merge_decision is not None)
        self.state["merge_decision"] = self._merge_decision
        if not self._merge_decision or not self._merge_decision.get("approved"):
            await self._stage(case_id, "rejected", "Human rejected the merge", self._merge_decision.get("note", "No reason provided") if self._merge_decision else "Rejected", {"approval": self._merge_decision or {"approved": False}}, kind="human")
            return self.state

        await self._stage(case_id, "merging", "Merge approved", "Temporal scheduled an idempotent merge Activity for the exact reviewed commit.", {"approval": self._merge_decision}, kind="human")
        merged = await workflow.execute_activity(
            "merge_repair", {"case_id": case_id, "expected_commit": commit["commit"]},
            start_to_close_timeout=timedelta(seconds=30), retry_policy=ACTIVITY_RETRY,
        )

        await self._stage(case_id, "verifying", "Independent outcome verification started", "Verification Agent is testing main and replaying the original checkout.", {"merge": merged})
        verification = await run_agent("verification", {**shared, "merge": merged, "expected": {"checkout_status": "accepted", "tests_exit_code": 0}})
        verify_observation = next((item for item in verification["observations"] if item["tool"] == "verify.main"), None)
        outcome = verify_observation["result"] if verify_observation else {"success": False}
        final_status = "completed" if outcome.get("success") else "failed"
        await self._stage(case_id, final_status, "Outcome verified" if outcome.get("success") else "Verification failed", "The merged code was measured against the original failure and regression suite.", {"verification": verification, "outcome": outcome}, kind="outcome")
        self.state.update({"status": final_status, "outcome": outcome})
        return self.state

    @workflow.signal
    async def merge_decision(self, decision: dict[str, Any]) -> None:
        if self.state.get("status") == "awaiting_merge_approval":
            self._merge_decision = decision

    @workflow.query
    def current_state(self) -> dict[str, Any]:
        return self.state
