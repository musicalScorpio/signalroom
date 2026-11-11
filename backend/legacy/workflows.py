"""Legacy backend Workflow for generic ambient signals."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class EventCaseWorkflow:
    def __init__(self) -> None:
        self.record: dict[str, Any] = {}
        self._approval: dict[str, Any] | None = None
        self._outcome_report: dict[str, Any] | None = None

    async def _persist(self) -> None:
        await workflow.execute_activity(
            "persist_projection", self.record,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    def _update_trace(self, stage: str, state: str, note: str) -> None:
        for item in self.record.get("decision", {}).get("trace", []):
            if item["stage"] == stage:
                item.update({"state": state, "note": note, "at": workflow.now().isoformat(timespec="seconds")})
                return

    @workflow.run
    async def run(self, event: dict[str, Any]) -> dict[str, Any]:
        now = event.get("received_at") or workflow.now().isoformat(timespec="seconds")
        event_id = event["id"]
        workflow_id = workflow.info().workflow_id
        self.record = {
            "id": event_id, "type": event["type"], "source": event.get("source", "manual.input"),
            "summary": event.get("summary", "An event arrived without a summary"), "payload": event.get("payload", {}),
            "received_at": now, "updated_at": now, "validation": {}, "context": {}, "policy": {},
            "decision": {"execution_engine": "temporal", "workflow_id": workflow_id, "trace": []},
            "status": "validating", "outcome": {"state": "pending", "contract": event.get("outcome_contract"), "action_result": None, "business_result": None},
            "feedback": {}, "model_mode": "pending",
        }
        await self._persist()

        validation = await workflow.execute_activity("validate_signal", event, start_to_close_timeout=timedelta(seconds=15))
        self.record["validation"] = validation
        if not validation["valid"]:
            issue = validation["issue"]
            self.record["status"] = "completed"
            self.record["decision"].update({
                "validation": validation,
                "trace": [
                    {"stage": "event", "state": "complete", "at": now, "note": f"Accepted from {self.record['source']}"},
                    {"stage": "context", "state": "blocked", "at": now, "note": issue},
                    {"stage": "policy", "state": "bypassed", "at": now, "note": "No policy decision"},
                    {"stage": "action", "state": "bypassed", "at": now, "note": "No action proposed"},
                    {"stage": "human", "state": "bypassed", "at": now, "note": "No approval requested"},
                    {"stage": "execution", "state": "bypassed", "at": now, "note": "No side effect"},
                    {"stage": "outcome", "state": "complete", "at": now, "note": "Signal rejected before execution"},
                    {"stage": "feedback", "state": "waiting", "at": now, "note": "Unrated"},
                ],
            })
            self.record["outcome"] = {"state": "complete", "contract": None, "action_result": {"state": "not_executed"}, "business_result": {"code": "signal_rejected", "label": "Signal rejected", "success": None, "note": issue, "reported_at": workflow.now().isoformat(timespec="seconds")}}
            await self._persist()
            return self.record

        context = await workflow.execute_activity("gather_context", event["type"], start_to_close_timeout=timedelta(seconds=30))
        model_result = await workflow.execute_activity(
            "decide_action", event,
            start_to_close_timeout=timedelta(seconds=45),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
        recommendation = model_result["recommendation"]
        run_mode = event.get("run_mode", "shadow")
        requires_human = (
            recommendation["risk"] == "high" or not recommendation["reversible"]
            or (run_mode == "guarded" and recommendation["risk"] == "medium")
        )
        self.record["context"] = context
        self.record["model_mode"] = model_result["model_mode"]
        self.record["policy"] = {"run_mode": run_mode, "requires_human": requires_human, "risk": recommendation["risk"], "reversible": recommendation["reversible"]}
        self.record["decision"].update({
            **recommendation, "validation": validation,
            "trace": [
                {"stage": "event", "state": "complete", "at": now, "note": f"Validated from {self.record['source']}"},
                {"stage": "context", "state": "complete", "at": now, "note": f"Snapshot assembled from {len(context['snapshot'])} sources"},
                {"stage": "policy", "state": "complete", "at": now, "note": f"{recommendation['risk'].title()} risk · {'reversible' if recommendation['reversible'] else 'irreversible'}"},
                {"stage": "action", "state": "complete", "at": now, "note": recommendation["label"]},
                {"stage": "human", "state": "waiting" if requires_human and run_mode != "shadow" else "bypassed", "at": now, "note": "Approval required" if requires_human else "Within delegated authority"},
                {"stage": "execution", "state": "waiting", "at": now, "note": "Waiting"},
                {"stage": "outcome", "state": "waiting", "at": now, "note": event["outcome_contract"]["question"]},
                {"stage": "feedback", "state": "waiting", "at": now, "note": "Unrated"},
            ],
        })

        if run_mode == "shadow":
            self.record["outcome"]["action_result"] = {"state": "withheld_shadow", "note": "No side effect in shadow mode."}
            self._update_trace("human", "bypassed", "Shadow mode")
            self._update_trace("execution", "shadow", "Side effect withheld in shadow mode")
        elif requires_human:
            self.record["status"] = "awaiting_approval"
            await self._persist()
            try:
                await workflow.wait_condition(lambda: self._approval is not None, timeout=timedelta(seconds=int(event.get("approval_timeout_seconds", 86400))))
            except asyncio.TimeoutError:
                self._approval = {"approved": False, "note": "Approval window expired"}
            if self._approval and self._approval.get("approved"):
                self._update_trace("human", "complete", self._approval.get("note") or "Action approved")
                self.record["outcome"]["action_result"] = await self._execute_action(workflow_id, event_id, recommendation["label"])
                self._update_trace("execution", "complete", "Action dispatched")
            else:
                note = (self._approval or {}).get("note") or "Action rejected"
                self.record["outcome"]["action_result"] = {"state": "rejected", "note": note}
                self._update_trace("human", "complete", note)
                self._update_trace("execution", "bypassed", "No side effect")
        else:
            self.record["outcome"]["action_result"] = await self._execute_action(workflow_id, event_id, recommendation["label"])
            self._update_trace("execution", "complete", "Action dispatched")

        self.record["status"] = "observing_outcome"
        await self._persist()
        try:
            await workflow.wait_condition(lambda: self._outcome_report is not None, timeout=timedelta(seconds=int(event.get("outcome_timeout_seconds", 604800))))
        except asyncio.TimeoutError:
            self._outcome_report = {"code": "outcome_unobserved", "note": "No business outcome arrived before the observation window closed"}

        contract = event["outcome_contract"]
        choice = next((item for item in contract["choices"] if item["code"] == self._outcome_report.get("code")), None)
        if choice:
            result = {**choice, "note": self._outcome_report.get("note", ""), "metrics": self._outcome_report.get("metrics", {}), "reported_at": workflow.now().isoformat(timespec="seconds")}
        else:
            result = {"code": "outcome_unobserved", "label": "Outcome unobserved", "success": False, "note": self._outcome_report.get("note", ""), "metrics": {}, "reported_at": workflow.now().isoformat(timespec="seconds")}
        self.record["outcome"].update({"state": "complete", "business_result": result})
        self.record["status"] = "completed"
        self._update_trace("outcome", "complete", result["label"])
        await self._persist()
        return self.record

    async def _execute_action(self, workflow_id: str, event_id: str, action: str) -> dict[str, Any]:
        return await workflow.execute_activity(
            "execute_action",
            {"event_id": event_id, "action": action, "idempotency_key": f"{workflow_id}:action"},
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )

    @workflow.signal
    async def approval_decision(self, decision: dict[str, Any]) -> None:
        if self.record.get("status") == "awaiting_approval":
            self._approval = decision

    @workflow.signal
    async def outcome_report(self, report: dict[str, Any]) -> None:
        if self.record.get("status") in {"awaiting_approval", "observing_outcome"}:
            self._outcome_report = report

    @workflow.query
    def current_state(self) -> dict[str, Any]:
        return self.record
