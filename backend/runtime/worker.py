#!/usr/bin/env python3
"""Run the SignalRoom Temporal Worker."""

import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from backend.orchestration.activities import (
    agent_model_turn_activity,
    agent_tool_activity,
    complete_agent_activity,
    merge_repair_activity,
    prepare_repair_case_activity,
    record_worker_incident_activity,
    update_case_stage_activity,
    execute_case_external_effect_activity,
    lookup_case_external_effect_activity,
    record_case_external_stage_activity,
)
from backend.orchestration.workflows import AgentWorkflow, IncidentCaseWorkflow
from backend.reliability.activities import (
    provider_find_payment_activity,
    provider_idempotent_create_activity,
    provider_non_idempotent_create_activity,
    record_reliability_stage_activity,
)
from backend.reliability.workflows import SideEffectReliabilityWorkflow
from backend.legacy.activities import (
    decide_action_activity,
    execute_action_activity,
    gather_context_activity,
    persist_projection_activity,
    validate_signal_activity,
)
from backend.legacy.workflows import EventCaseWorkflow


async def main() -> None:
    address = os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    task_queue = os.getenv("TEMPORAL_TASK_QUEUE", "signalroom-events")
    client = await Client.connect(address)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[EventCaseWorkflow, AgentWorkflow, IncidentCaseWorkflow, SideEffectReliabilityWorkflow],
        activities=[
            validate_signal_activity, gather_context_activity, decide_action_activity, persist_projection_activity, execute_action_activity,
            prepare_repair_case_activity, agent_model_turn_activity, agent_tool_activity, complete_agent_activity,
            update_case_stage_activity, merge_repair_activity, record_worker_incident_activity,
            execute_case_external_effect_activity, lookup_case_external_effect_activity,
            record_case_external_stage_activity,
            record_reliability_stage_activity, provider_idempotent_create_activity,
            provider_non_idempotent_create_activity, provider_find_payment_activity,
        ],
    )
    print(f"SignalRoom Temporal Worker listening on {task_queue} via {address}")
    await worker.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
