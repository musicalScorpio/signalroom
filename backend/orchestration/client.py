"""Synchronous bridge from the stdlib HTTP server to Temporal's async client."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from backend.orchestration.workflows import AgentWorkflow, IncidentCaseWorkflow
from backend.reliability.workflows import SideEffectReliabilityWorkflow
from backend.legacy.workflows import EventCaseWorkflow


class TemporalBridge:
    def __init__(self) -> None:
        self.address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
        self.task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "signalroom-events")
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="signalroom-temporal-client")
        self.thread.start()
        self._client: Client | None = None

    async def _connect(self) -> Client:
        if self._client is None:
            self._client = await asyncio.wait_for(Client.connect(self.address), timeout=2.5)
        return self._client

    def _run(self, coroutine: Any, timeout: float = 8.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=timeout)

    def available(self) -> bool:
        if os.getenv("SIGNALROOM_TEMPORAL", "auto").lower() in {"0", "false", "off"}:
            return False
        try:
            self._run(self._connect(), timeout=3.0)
            return True
        except Exception:
            self._client = None
            return False

    def start_event(self, event: dict[str, Any]) -> bool:
        return bool(self._run(self._start_event(event)))

    async def _start_event(self, event: dict[str, Any]) -> bool:
        client = await self._connect()
        try:
            await client.start_workflow(
                EventCaseWorkflow.run, event,
                id=f"signalroom-{event['id']}", task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return True
        except WorkflowAlreadyStartedError:
            return False

    def start_incident(self, case: dict[str, Any]) -> bool:
        return bool(self._run(self._start_incident(case), timeout=12.0))

    async def _start_incident(self, case: dict[str, Any]) -> bool:
        client = await self._connect()
        try:
            await client.start_workflow(
                IncidentCaseWorkflow.run, case,
                id=f"signalroom-case-{case['case_id']}", task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return True
        except WorkflowAlreadyStartedError:
            return False

    def start_reliability_run(self, payload: dict[str, Any]) -> bool:
        return bool(self._run(self._start_reliability_run(payload), timeout=12.0))

    async def _start_reliability_run(self, payload: dict[str, Any]) -> bool:
        client = await self._connect()
        try:
            await client.start_workflow(
                SideEffectReliabilityWorkflow.run,
                payload,
                id=f"signalroom-side-effect-{payload['run_id']}",
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            return True
        except WorkflowAlreadyStartedError:
            return False

    def query_reliability_run(self, run_id: str) -> dict[str, Any]:
        return self._run(self._query_reliability_run(run_id))

    async def _query_reliability_run(self, run_id: str) -> dict[str, Any]:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-side-effect-{run_id}")
        return await handle.query(SideEffectReliabilityWorkflow.current_state)

    def decide_merge(self, case_id: str, approved: bool, note: str = "") -> None:
        self._run(self._decide_merge(case_id, approved, note))

    async def _decide_merge(self, case_id: str, approved: bool, note: str) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-case-{case_id}")
        await handle.signal(IncidentCaseWorkflow.merge_decision, {"approved": approved, "note": note})

    def query_incident(self, case_id: str) -> dict[str, Any]:
        return self._run(self._query_incident(case_id))

    async def _query_incident(self, case_id: str) -> dict[str, Any]:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-case-{case_id}")
        return await handle.query(IncidentCaseWorkflow.current_state)

    def query_agent(self, case_id: str, agent_id: str) -> dict[str, Any]:
        """Read the replayed state of one durable child Agent Workflow."""
        return self._run(self._query_agent(case_id, agent_id))

    async def _query_agent(self, case_id: str, agent_id: str) -> dict[str, Any]:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-{case_id}-{agent_id}")
        return await handle.query(AgentWorkflow.current_state)

    def approve(self, event_id: str, approved: bool, note: str = "") -> None:
        self._run(self._approve(event_id, approved, note))

    async def _approve(self, event_id: str, approved: bool, note: str) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-{event_id}")
        await handle.signal(EventCaseWorkflow.approval_decision, {"approved": approved, "note": note})

    def report_outcome(self, event_id: str, report: dict[str, Any]) -> None:
        self._run(self._report_outcome(event_id, report))

    async def _report_outcome(self, event_id: str, report: dict[str, Any]) -> None:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-{event_id}")
        await handle.signal(EventCaseWorkflow.outcome_report, report)

    def query(self, event_id: str) -> dict[str, Any]:
        return self._run(self._query(event_id))

    async def _query(self, event_id: str) -> dict[str, Any]:
        client = await self._connect()
        handle = client.get_workflow_handle(f"signalroom-{event_id}")
        return await handle.query(EventCaseWorkflow.current_state)
