"""Temporal Activities for the local multi-agent software repair pipeline."""

from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity

from agent_harness import load_definitions, model_turn
from agent_store import case_store
from repair_lab import (
    apply_repair,
    commit_repair,
    git_history,
    inspect_contract,
    logs,
    merge,
    prepare_case,
    reproduce,
    review_diff,
    run_tests,
    verify_main,
)


TOOLS = {
    "tests.reproduce": reproduce,
    "logs.read": logs,
    "contract.inspect": inspect_contract,
    "git.history": git_history,
    "code.patch": apply_repair,
    "tests.run": run_tests,
    "git.commit": commit_repair,
    "review.diff": review_diff,
    "verify.main": verify_main,
}


@activity.defn(name="prepare_repair_case")
async def prepare_repair_case_activity(input: dict[str, Any]) -> dict[str, Any]:
    case_id = input["case_id"]
    lab = prepare_case(case_id)
    definitions = load_definitions()
    store = case_store()
    store.patch(case_id, {"status": "investigating", "lab": lab, "context": {"event": lab["event"], "original_failure": lab["failure"], "repo": lab["repo"]}})
    store.timeline(case_id, "event", "Runtime failure captured", "Order Service failed against Inventory contract v2.", lab["event"])
    store.timeline(case_id, "temporal", "Temporal case started", "Parent Workflow scheduled two investigation child Workflows in parallel.")
    return {"lab": lab, "definitions": definitions}


@activity.defn(name="agent_model_turn")
async def agent_model_turn_activity(input: dict[str, Any]) -> dict[str, Any]:
    store = case_store()
    agent_id = input["agent_id"]
    store.agent(input["case_id"], agent_id, status="reasoning", turn=input["turn"], active_tool=None)
    store.timeline(
        input["case_id"], "model", f"{input['definition']['name']} reasoning turn {input['turn']} started",
        "Context and recorded observations sent to the bounded local model harness.", status="running",
    )
    await asyncio.sleep(float(input.get("step_delay", 0.25)))
    command = await asyncio.to_thread(model_turn, input["definition"], input["context"], input["observations"])
    store.agent(input["case_id"], agent_id, status="reasoning", turn=input["turn"], model_mode=command.get("model_mode"), last_summary=command.get("summary", ""))
    store.timeline(input["case_id"], "model", f"{input['definition']['name']} reasoning turn {input['turn']}", command.get("summary", "Structured command returned."), command)
    return command


@activity.defn(name="agent_tool")
async def agent_tool_activity(input: dict[str, Any]) -> dict[str, Any]:
    tool = input["tool"]
    if tool not in input["definition"]["tools"] or tool not in TOOLS:
        raise ValueError(f"Tool {tool} is not allowed for {input['agent_id']}")
    store = case_store()
    store.agent(input["case_id"], input["agent_id"], status="using_tool", active_tool=tool)
    store.timeline(input["case_id"], "tool", f"{input['definition']['name']} called {tool}", "Executing a scoped local tool.", input.get("arguments", {}), status="running")
    result = await asyncio.to_thread(TOOLS[tool], input["case_id"])
    observation = {"tool": tool, "arguments": input.get("arguments", {}), "result": result}
    current = store.get(input["case_id"]) or {}
    agent = current.get("agents", {}).get(input["agent_id"], {})
    observations = [*agent.get("observations", []), observation]
    store.agent(input["case_id"], input["agent_id"], status="reasoning", active_tool=None, observations=observations)
    store.timeline(input["case_id"], "tool_result", f"{tool} returned evidence", "Result recorded in Temporal history and the SignalRoom projection.", result)
    return observation


@activity.defn(name="complete_agent")
async def complete_agent_activity(input: dict[str, Any]) -> dict[str, Any]:
    store = case_store()
    store.agent(input["case_id"], input["agent_id"], status="completed", active_tool=None, result=input["result"], observations=input["observations"])
    store.timeline(input["case_id"], "handoff", f"{input['definition']['name']} completed", input["result"].get("summary", "Evidence packet ready."), input["result"])
    return input["result"]


@activity.defn(name="update_case_stage")
async def update_case_stage_activity(input: dict[str, Any]) -> dict[str, Any]:
    store = case_store()
    changes = {"status": input["status"]}
    changes.update(input.get("changes", {}))
    record = store.patch(input["case_id"], changes)
    store.timeline(input["case_id"], input.get("kind", "temporal"), input["title"], input.get("detail", ""), input.get("data"), status=input.get("timeline_status", "complete"))
    return record or {}


@activity.defn(name="merge_repair")
async def merge_repair_activity(input: dict[str, Any]) -> dict[str, Any]:
    result = merge(input["case_id"], input["expected_commit"])
    store = case_store()
    store.patch(input["case_id"], {"merge": result})
    store.timeline(input["case_id"], "git", "Approved commit merged to main", "The merge Activity verified the exact approved SHA before changing main.", result)
    return result


@activity.defn(name="record_worker_incident")
async def record_worker_incident_activity(input: dict[str, Any]) -> dict[str, Any]:
    case_store().timeline(input["case_id"], "worker", input["title"], input.get("detail", ""), input.get("data"), status=input.get("status", "complete"))
    return {"recorded": True}
