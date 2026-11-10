"""Synchronous bridge from the stdlib HTTP server to Temporal's async client."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from temporal_workflows import EventCaseWorkflow


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
