"""Temporal Activities for the local side-effect reliability lab."""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from backend.reliability.provider_client import (
    create_idempotent,
    create_non_idempotent,
    find_payments,
)
from backend.storage.reliability_store import reliability_store


def _update_agent(
    store: Any,
    run_id: str,
    agent_id: str,
    *,
    status: str,
    summary: str,
    turn: int | None = None,
    active_tool: str | None = None,
    observation: dict[str, Any] | None = None,
) -> None:
    """Update the operator projection for a policy-bound side-effect agent."""
    def update(record: dict[str, Any]) -> None:
        agent = record.setdefault("agents", {}).setdefault(agent_id, {"id": agent_id, "observations": []})
        agent.update({"status": status, "last_summary": summary, "active_tool": active_tool})
        if turn is not None:
            agent["turn"] = turn
        if observation:
            observations = agent.setdefault("observations", [])
            if not any(item.get("id") == observation.get("id") for item in observations):
                observations.append(observation)

    store.mutate(run_id, update)


async def _danger_window(input: dict[str, Any], provider_result: dict[str, Any]) -> None:
    """Hold the Activity open so a human can kill the Worker after the effect.

    The heartbeat proves only that the Worker reached this point. The provider's
    own database remains the authority for whether the external effect exists.
    """
    store = reliability_store()
    attempt = activity.info().attempt
    payment = provider_result["payment"]
    store.patch(input["run_id"], {
        "status": "danger_window",
        "activity_attempt": attempt,
        "provider_result": provider_result,
        "external_payment_id": payment["id"],
        "danger_window": True,
    })
    _update_agent(
        store, input["run_id"], "action_agent", status="waiting_for_receipt", turn=attempt,
        summary="Provider committed, but the create-tool result is not durable yet. Kill the Worker now.",
    )
    store.timeline(
        input["run_id"], "provider", "Provider committed the payment",
        "The external effect exists, but the Activity result has not yet been recorded in Temporal Event History. Kill the Worker now to exercise the ambiguity window.",
        {"attempt": attempt, "payment": payment, "heartbeat_is_not_a_receipt": True},
        status="danger", event_id=f"provider-effect-{input['run_id']}-{attempt}",
    )
    hold_seconds = max(0.0, float(input.get("hold_seconds", 12.0)))
    elapsed = 0.0
    while elapsed < hold_seconds:
        activity.heartbeat({
            "phase": "external_effect_committed",
            "payment_id": payment["id"],
            "attempt": attempt,
        })
        interval = min(0.5, hold_seconds - elapsed)
        await asyncio.sleep(interval)
        elapsed += interval


@activity.defn(name="record_reliability_stage")
async def record_reliability_stage_activity(input: dict[str, Any]) -> dict[str, Any]:
    store = reliability_store()
    changes = {"status": input["status"], "danger_window": False, **input.get("changes", {})}
    record = store.patch(input["run_id"], changes)
    store.timeline(
        input["run_id"], input.get("kind", "temporal"), input["title"],
        input.get("detail", ""), input.get("data"), input.get("timeline_status", "complete"),
        event_id=input.get("event_id"),
    )
    if input["status"] in {"completed", "failed", "uncertain"}:
        terminal = input["status"]
        resolution = input.get("changes", {}).get("resolution", terminal)
        _update_agent(
            store, input["run_id"], "action_agent",
            status="completed" if terminal == "completed" else "stopped" if terminal == "uncertain" else "failed",
            summary=("Create phase closed; the recorded outcome contains the authoritative result."
                     if terminal == "completed" else "Create will not be called again without a new human decision."),
            active_tool=None,
        )
        _update_agent(
            store, input["run_id"], "recovery_agent",
            status="completed" if terminal == "completed" else "needs_human" if terminal == "uncertain" else "failed",
            summary=f"Outcome decision: {resolution.replace('_', ' ')}.", active_tool=None,
        )
        record = store.get(input["run_id"])
    return record or {}


@activity.defn(name="provider_idempotent_create")
async def provider_idempotent_create_activity(input: dict[str, Any]) -> dict[str, Any]:
    attempt = activity.info().attempt
    store = reliability_store()
    store.patch(input["run_id"], {"status": "calling_provider", "activity_attempt": attempt, "danger_window": False})
    request = {
        "agent": "Payment Action Agent", "tool": "payments.create_idempotent",
        "method": "POST", "route": "/payments/idempotent",
        "headers": {"Idempotency-Key": input["operation_id"]},
        "body": input["payload"], "attempt": attempt,
    }
    _update_agent(
        store, input["run_id"], "action_agent", status="running", turn=attempt,
        active_tool="payments.create_idempotent",
        summary=f"Attempt {attempt}: calling create with the same durable Idempotency-Key.",
        observation={"id": f"native-call-{attempt}", "tool": "payments.create_idempotent", "request": request},
    )
    store.timeline(
        input["run_id"], "agent",
        f"Payment Action Agent {'retries' if attempt > 1 else 'calls'} create with Idempotency-Key",
        "A retry repeats the HTTP request, but it reuses the exact same operation identity.",
        request,
        status="running", event_id=f"native-call-{input['run_id']}-{attempt}",
    )
    result = await asyncio.to_thread(create_idempotent, input["payload"], input["operation_id"])
    result["activity_attempt"] = attempt
    result["idempotency_key"] = input["operation_id"]
    if result.get("replayed"):
        _update_agent(
            store, input["run_id"], "action_agent", status="completed", turn=attempt, active_tool=None,
            summary="Provider replayed the original receipt; the retry created no second payment.",
            observation={"id": f"native-result-{attempt}", "tool": "payments.create_idempotent", "result": result},
        )
        store.timeline(
            input["run_id"], "provider", "Provider replayed the original response",
            "The agent called POST again with the same key. The provider returned the first receipt and created nothing new.", result,
            event_id=f"native-replay-{input['run_id']}-{attempt}",
        )
    else:
        await _danger_window(input, result)
    return result


@activity.defn(name="provider_non_idempotent_create")
async def provider_non_idempotent_create_activity(input: dict[str, Any]) -> dict[str, Any]:
    attempt = activity.info().attempt
    store = reliability_store()
    store.patch(input["run_id"], {"status": "calling_provider", "activity_attempt": attempt, "danger_window": False})
    request = {
        "agent": "Payment Action Agent", "tool": "payments.create_once",
        "method": "POST", "route": "/payments/non-idempotent", "headers": {},
        "body": input["payload"], "attempt": attempt, "maximum_attempts": 1,
    }
    _update_agent(
        store, input["run_id"], "action_agent", status="running", turn=attempt,
        active_tool="payments.create_once",
        summary="Calling a non-idempotent create exactly once; policy forbids a blind retry.",
        observation={"id": f"unsafe-call-{attempt}", "tool": "payments.create_once", "request": request},
    )
    store.timeline(
        input["run_id"], "agent", "Payment Action Agent calls the one-shot create tool",
        "The provider creates a new payment on every call, so this tool is configured with maximumAttempts=1.",
        request,
        status="running", event_id=f"unsafe-call-{input['run_id']}-{attempt}",
    )
    result = await asyncio.to_thread(create_non_idempotent, input["payload"])
    result["activity_attempt"] = attempt
    await _danger_window(input, result)
    return result


@activity.defn(name="provider_find_payment")
async def provider_find_payment_activity(input: dict[str, Any]) -> dict[str, Any]:
    result = await asyncio.to_thread(find_payments, input["payment_code"], True)
    store = reliability_store()
    check = int(input.get("check", 0))
    store.patch(input["run_id"], {"validation_check": check, "provider_match_count": result["count"]})
    request = {
        "agent": "Outcome Recovery Agent", "tool": "payments.lookup", "method": "GET",
        "route": f"/payments?payment_code={input['payment_code']}", "check": check,
        "decision": "Do not call create again; reconcile using a repeatable read." if check else "Preflight before create.",
        "result_count": result["count"],
    }
    _update_agent(
        store, input["run_id"], "recovery_agent", status="running", turn=check + 1,
        active_tool="payments.lookup",
        summary=("Create suppressed. Checking provider state with a safe GET."
                 if check else "Checking for an existing effect before the first create."),
        observation={"id": f"validation-{check}", "tool": "payments.lookup", "request": request, "result": result},
    )
    store.timeline(
        input["run_id"], "agent",
        (f"Outcome Recovery Agent suppresses a second create and calls GET #{check}"
         if check else "Outcome Recovery Agent performs the preflight GET"),
        "This read is safe to repeat. It gathers evidence without creating another payment.",
        {**request, "provider_result": result},
        event_id=f"validation-{input['run_id']}-{check}",
    )
    return result

