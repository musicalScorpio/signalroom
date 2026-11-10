"""Temporal Activities: all nondeterministic I/O for SignalRoom lives here."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from temporalio import activity

from signalroom_core import DEFAULT_DB, Engine, Store, context_for, utc_now, validate_signal


def store() -> Store:
    return Store(Path(os.getenv("SIGNALROOM_DB", str(DEFAULT_DB))))


@activity.defn(name="validate_signal")
async def validate_signal_activity(event: dict[str, Any]) -> dict[str, Any]:
    return validate_signal(event)


@activity.defn(name="gather_context")
async def gather_context_activity(event_type: str) -> dict[str, Any]:
    return context_for(event_type)


@activity.defn(name="decide_action")
async def decide_action_activity(event: dict[str, Any]) -> dict[str, Any]:
    recommendation, model_mode = Engine(store()).recommend(event)
    return {"recommendation": recommendation, "model_mode": model_mode}


@activity.defn(name="persist_projection")
async def persist_projection_activity(record: dict[str, Any]) -> dict[str, Any]:
    record["updated_at"] = utc_now()
    store().upsert(record)
    return {"persisted": True, "event_id": record["id"]}


@activity.defn(name="execute_action")
async def execute_action_activity(input: dict[str, Any]) -> dict[str, Any]:
    return store().record_effect(input["idempotency_key"], input["event_id"], input["action"])
