"""The backend Temporal orchestrator for parent/child multi-agent repair."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


ACTIVITY_RETRY = RetryPolicy(maximum_attempts=3)
NATIVE_EXTERNAL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=4), maximum_attempts=4,
)
AT_MOST_ONCE = RetryPolicy(maximum_attempts=1)
LOCAL_ACTIVITY_HEARTBEAT_TIMEOUT = timedelta(seconds=30)


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
        self.state = {
            "agent_id": input["agent_id"],
            "agent_name": definition.get("name", input["agent_id"]),
            "status": "running",
            "turn": 0,
            "active_tool": None,
            # Exact durable child-Workflow input exposed by query so the UI can
            # prove what the replacement agent received after Temporal replay.
            "assignment_context": input.get("context", {}),
            "observations": observations,
        }
        for turn in range(1, int(definition["max_turns"]) + 1):
            self.state.update({"status": "reasoning", "turn": turn})
            command = await workflow.execute_activity(
                "agent_model_turn",
                {**input, "turn": turn, "observations": observations},
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=LOCAL_ACTIVITY_HEARTBEAT_TIMEOUT, retry_policy=ACTIVITY_RETRY,
            )
            if command.get("type") == "call_tool":
                self.state.update({"status": "using_tool", "active_tool": command["tool"]})
                observation = await workflow.execute_activity(
                    "agent_tool",
                    {**input, "tool": command["tool"], "arguments": command.get("arguments", {})},
                    start_to_close_timeout=timedelta(minutes=2),
                    heartbeat_timeout=LOCAL_ACTIVITY_HEARTBEAT_TIMEOUT, retry_policy=ACTIVITY_RETRY,
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

    async def _external_stage(self, case_id: str, status: str, title: str, detail: str, **extra: Any) -> None:
        await workflow.execute_activity(
            "record_case_external_stage",
            {"case_id": case_id, "status": status, "title": title, "detail": detail, **extra},
            start_to_close_timeout=timedelta(seconds=15), retry_policy=ACTIVITY_RETRY,
        )
        self.state["status"] = status

    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        case_id = input["case_id"]
        try:
            return await self._run_pipeline(input)
        except Exception as error:
            failure = {
                "type": type(error).__name__,
                "message": str(error),
                "stage": self.state.get("status", "unknown"),
            }
            try:
                await self._stage(
                    case_id, "failed", "Agent pipeline failed",
                    "Temporal closed the pipeline after its retry policy was exhausted. See the recorded failure for the exact stage.",
                    {"failure": failure}, kind="temporal",
                )
            except Exception:
                # Preserve the original Workflow failure if projection recording is also unavailable.
                pass
            self.state.update({"status": "failed", "failure": failure})
            raise

    async def _run_pipeline(self, input: dict[str, Any]) -> dict[str, Any]:
        case_id = input["case_id"]
        self.state = {"case_id": case_id, "status": "preparing", "merge_decision": None}
        prepared = await workflow.execute_activity(
            "prepare_repair_case", input,
            start_to_close_timeout=timedelta(seconds=45), retry_policy=ACTIVITY_RETRY,
        )
        definitions = prepared["definitions"]
        context = {"case_id": case_id, **prepared["lab"], "side_effect_strategy": input.get("side_effect_strategy", "none")}
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

        strategy = input.get("side_effect_strategy", "none")
        if strategy != "none":
            external_input = {
                "case_id": case_id,
                "side_effect_strategy": strategy,
                "operation_id": input["operation_id"],
                "payment_code": input["payment_code"],
                "amount_cents": int(input["amount_cents"]),
                "auto_crash": bool(input.get("auto_crash", False)),
                "hold_seconds": float(input.get("hold_seconds", 20.0)),
                "reviewed_commit": commit["commit"],
            }
            await self._external_stage(
                case_id, "proving_external_effect", "Reviewed branch entered the external-effect proof",
                "The Side-Effect Execution Agent is running the exact reviewed branch before the merge gate.",
                changes={"strategy": strategy, "reviewed_commit": commit["commit"]},
                kind="agent", event_id=f"external-proof-start-{case_id}",
            )
            external_evidence: dict[str, Any]
            if strategy == "native_idempotency":
                result = await workflow.execute_activity(
                    "execute_case_external_effect", external_input,
                    start_to_close_timeout=timedelta(seconds=60), heartbeat_timeout=timedelta(seconds=3),
                    retry_policy=NATIVE_EXTERNAL_RETRY,
                )
                external_evidence = {
                    "strategy": strategy,
                    "resolution": "provider_idempotency_replay" if result.get("provider_result", {}).get("replayed") else "first_attempt_completed",
                    "result": result,
                    "duplicate_count": 0,
                }
            elif strategy == "at_most_once":
                try:
                    result = await workflow.execute_activity(
                        "execute_case_external_effect", external_input,
                        start_to_close_timeout=timedelta(seconds=60), heartbeat_timeout=timedelta(seconds=3),
                        retry_policy=AT_MOST_ONCE,
                    )
                    external_evidence = {"strategy": strategy, "resolution": "first_attempt_completed", "result": result, "duplicate_count": 0}
                except ActivityError as error:
                    external_evidence = {
                        "strategy": strategy, "resolution": "manual_reconciliation_required",
                        "failure": str(error), "duplicate_count": 0,
                    }
            elif strategy == "validation":
                preflight = await workflow.execute_activity(
                    "lookup_case_external_effect", {**external_input, "check": 0},
                    start_to_close_timeout=timedelta(seconds=12), retry_policy=ACTIVITY_RETRY,
                )
                if preflight.get("count", 0) == 1:
                    external_evidence = {"strategy": strategy, "resolution": "preflight_validation", "result": preflight["payments"][0], "duplicate_count": 0}
                elif preflight.get("count", 0) > 1:
                    external_evidence = {"strategy": strategy, "resolution": "duplicates_detected", "result": preflight, "duplicate_count": preflight["count"] - 1}
                else:
                    try:
                        result = await workflow.execute_activity(
                            "execute_case_external_effect", external_input,
                            start_to_close_timeout=timedelta(seconds=60), heartbeat_timeout=timedelta(seconds=3),
                            retry_policy=AT_MOST_ONCE,
                        )
                        external_evidence = {"strategy": strategy, "resolution": "first_attempt_completed", "result": result, "duplicate_count": 0}
                    except ActivityError:
                        external_evidence = {"strategy": strategy, "resolution": "manual_reconciliation_required", "duplicate_count": 0}
                        await self._external_stage(
                            case_id, "reconciling_external_effect", "Replacement agent suppressed a second POST",
                            "Temporal applied maximumAttempts=1 to the non-idempotent write. Only safe GET checks may now run.",
                            changes={"backoff_seconds": [1, 2, 4]}, kind="temporal",
                            event_id=f"external-reconcile-{case_id}",
                        )
                        for check, delay in enumerate((1, 2, 4), start=1):
                            await workflow.sleep(timedelta(seconds=delay))
                            lookup = await workflow.execute_activity(
                                "lookup_case_external_effect", {**external_input, "check": check},
                                start_to_close_timeout=timedelta(seconds=12), retry_policy=ACTIVITY_RETRY,
                            )
                            if lookup.get("count", 0) == 1:
                                external_evidence = {
                                    "strategy": strategy, "resolution": "validation_recovered",
                                    "result": lookup["payments"][0], "validation_checks": check, "duplicate_count": 0,
                                }
                                break
                            if lookup.get("count", 0) > 1:
                                external_evidence = {
                                    "strategy": strategy, "resolution": "duplicates_detected",
                                    "result": lookup, "validation_checks": check, "duplicate_count": lookup["count"] - 1,
                                }
                                break
            else:
                raise ValueError(f"Unknown side-effect strategy: {strategy}")

            evidence["external_effect"] = external_evidence
            self.state["context_checkpoint"].update({
                "side_effect_strategy": strategy,
                "operation_id": input["operation_id"],
                "external_resolution": external_evidence["resolution"],
            })
            needs_human = external_evidence["resolution"] in {"manual_reconciliation_required", "duplicates_detected"}
            await self._external_stage(
                case_id, "external_effect_proven", "External-effect evidence recorded",
                ("The provider effect and recovery decision are now attached to the exact reviewed commit."
                 if not needs_human else "The unsafe POST was not retried; the unresolved provider outcome is explicitly surfaced for human review."),
                changes={"evidence": external_evidence, "resolution": external_evidence["resolution"]},
                kind="outcome", agent_status="needs_human" if needs_human else "completed",
                agent_summary=f"Recovery decision: {external_evidence['resolution'].replace('_', ' ')}.",
                event_id=f"external-proof-complete-{case_id}",
            )
        else:
            await self._external_stage(
                case_id, "external_effect_skipped", "No external API selected for this repair",
                "This run proves code durability only; no provider side effect is part of the reviewed change.",
                changes={"resolution": "not_applicable"}, kind="agent", agent_status="completed",
                agent_summary="No external API was selected; side-effect execution was intentionally skipped.",
                event_id=f"external-proof-skipped-{case_id}",
            )

        await self._stage(
            case_id, "awaiting_merge_approval", "Human merge approval required",
            "Agents produced a real branch, commit, diff, tests, and any selected provider proof. Temporal is durably waiting.",
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
