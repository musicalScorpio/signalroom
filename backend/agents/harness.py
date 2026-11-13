"""Typed local Ollama agent harness with a deterministic safe fallback."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from backend.ollama_runtime import ollama_status, selected_ollama_model

DEFINITIONS_PATH = Path(__file__).with_name("definitions.json")
_OLLAMA_LOCK = threading.Lock()

TOOL_CONTRACTS: dict[str, dict[str, Any]] = {
    "tests.reproduce": {"purpose": "Run the original failing test suite.", "arguments": {}},
    "logs.read": {"purpose": "Read the captured runtime failure log.", "arguments": {}},
    "contract.inspect": {"purpose": "Read both service contracts and the current caller.", "arguments": {}},
    "git.history": {"purpose": "Read recent repository history.", "arguments": {}},
    "code.inspect": {"purpose": "Read the bounded editable source, dependency, tests, and runtime log.", "arguments": {}},
    "code.patch": {
        "purpose": "Apply source code authored by the model after validation.",
        "arguments": {
            "file": "must equal order_service.py",
            "replacement_content": "complete syntactically valid Python source authored by you",
            "rationale": "concise evidence-based explanation of the change",
        },
    },
    "tests.run": {"purpose": "Run the complete repair-lab test suite.", "arguments": {}},
    "git.commit": {"purpose": "Commit the tested model-authored source on the isolated branch.", "arguments": {}},
    "review.diff": {"purpose": "Inspect the exact committed diff.", "arguments": {}},
    "verify.main": {"purpose": "Run tests and the original checkout on merged main.", "arguments": {}},
}


def ollama_think_setting(model_name: str) -> bool | str:
    """Use the model's supported reasoning control without exhausting output tokens."""
    configured = os.getenv("OLLAMA_THINK")
    if configured is not None:
        normalized = configured.strip().lower()
        if normalized in {"0", "false", "off", "no"}:
            return False
        if normalized in {"1", "true", "on", "yes"}:
            return True
        return normalized
    # gpt-oss emits a reasoning channel even when think=false. Low keeps enough
    # of the generation budget for the required JSON and replacement source.
    return "low" if model_name.lower().startswith("gpt-oss") else False


def parse_model_json(content: str) -> dict[str, Any]:
    """Parse one complete JSON object while tolerating harmless trailing chatter."""
    text = content.strip()
    if text.startswith("```"):
        first_line, _, remainder = text.partition("\n")
        text = remainder.rsplit("```", 1)[0].strip() if first_line.lower() in {"```", "```json"} else text
    start = text.find("{")
    if start < 0:
        raise ValueError("Model response does not contain a JSON object")
    value, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("Model response JSON must be an object")
    return value


def ollama_output_budget(repair_generation: bool) -> int:
    """Keep local turns concise while leaving enough room for a source replacement."""
    configured = os.getenv("OLLAMA_NUM_PREDICT")
    if configured:
        return int(configured)
    return 4096 if repair_generation else 512


def load_definitions() -> dict[str, dict[str, Any]]:
    definitions = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
    required = {"name", "purpose", "system_prompt", "tools", "required_tools", "max_turns"}
    for agent_id, definition in definitions.items():
        missing = required - definition.keys()
        if missing:
            raise ValueError(f"Agent {agent_id} missing fields: {sorted(missing)}")
        if not set(definition["required_tools"]).issubset(definition["tools"]):
            raise ValueError(f"Agent {agent_id} requires tools outside its allowlist")
    return definitions


def _fallback(definition: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    completed = {item["tool"] for item in observations}
    remaining = [tool for tool in definition["required_tools"] if tool not in completed]
    if remaining:
        return {"type": "call_tool", "tool": remaining[0], "arguments": {}, "summary": f"Collect required evidence with {remaining[0]}", "model_mode": "deterministic-fallback"}
    return {
        "type": "complete",
        "result": {
            "summary": f"{definition['name']} completed its bounded assignment.",
            "evidence_count": len(observations),
            "tools_used": sorted(completed),
            "confidence": 0.94 if observations else 0.5,
        },
        "summary": "Required evidence collected; handoff is ready.",
        "model_mode": "deterministic-fallback",
    }


def _latest_tool_index(observations: list[dict[str, Any]], tool: str) -> int:
    return max((index for index, item in enumerate(observations) if item.get("tool") == tool), default=-1)


def _command_error(command: dict[str, Any], definition: dict[str, Any], observations: list[dict[str, Any]]) -> str | None:
    command_type = command.get("type")
    completed = {item.get("tool") for item in observations}
    remaining = [tool for tool in definition["required_tools"] if tool not in completed]
    if command_type == "complete":
        if remaining:
            return f"Required tools remain: {remaining}"
        if not isinstance(command.get("result", {}), dict):
            return "Completion result must be an object"
        return None
    if command_type != "call_tool":
        return "Command type must be call_tool or complete"

    tool = command.get("tool")
    if tool not in definition["tools"]:
        return f"Tool {tool!r} is not allowed"
    arguments = command.get("arguments", {})
    if not isinstance(arguments, dict):
        return "Tool arguments must be an object"

    if definition.get("requires_model"):
        patch_index = _latest_tool_index(observations, "code.patch")
        test_index = _latest_tool_index(observations, "tests.run")
        if tool == "code.inspect" and "code.inspect" in completed:
            return "Repository inspection is already recorded"
        if tool == "code.patch":
            if "code.inspect" not in completed:
                return "Inspect the bounded repository before authoring code"
            if arguments.get("file") != "order_service.py":
                return "code.patch may only replace order_service.py"
            replacement = arguments.get("replacement_content")
            if not isinstance(replacement, str) or not replacement.strip():
                return "code.patch requires model-authored replacement_content"
        elif tool == "tests.run":
            if patch_index < 0:
                return "Author a patch before running repair tests"
            if test_index > patch_index:
                return "The current patch has already been tested"
        elif tool == "git.commit":
            if patch_index < 0 or test_index < patch_index:
                return "Test the latest model-authored patch before committing"
            latest_tests = observations[test_index].get("result", {})
            if latest_tests.get("exit_code") != 0:
                return "The latest test run must pass before committing"
        elif tool not in {"code.inspect"}:
            return f"Unsupported model-required tool: {tool}"
        return None

    if tool in completed:
        return f"Tool {tool} already produced evidence"
    return None


def _repair_control_command(definition: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Keep deterministic workflow mechanics outside the model; return None when Ollama must author code."""
    try:
        model_name = selected_ollama_model()
    except RuntimeError:
        model_name = "unavailable"
    authored_mode = f"ollama/{model_name} + guarded-tools"
    inspect_index = _latest_tool_index(observations, "code.inspect")
    patch_index = _latest_tool_index(observations, "code.patch")
    test_index = _latest_tool_index(observations, "tests.run")
    commit_index = _latest_tool_index(observations, "git.commit")
    if inspect_index < 0:
        return {
            "type": "call_tool", "tool": "code.inspect", "arguments": {},
            "summary": "Load the bounded source, tests, dependency, and failure log before asking Ollama to repair code.",
            "model_mode": "guarded-repair-controller",
        }
    latest_patch_failed = patch_index >= 0 and observations[patch_index].get("result", {}).get("success") is False
    if patch_index < 0 or latest_patch_failed or (test_index > patch_index and observations[test_index].get("result", {}).get("exit_code") != 0):
        return None
    if test_index < patch_index:
        return {
            "type": "call_tool", "tool": "tests.run", "arguments": {},
            "summary": "Run the real test suite against Ollama's latest source replacement.",
            "model_mode": authored_mode,
        }
    if commit_index < test_index:
        return {
            "type": "call_tool", "tool": "git.commit", "arguments": {},
            "summary": "The latest model-authored source passed; commit the exact tested content.",
            "model_mode": authored_mode,
        }
    return {
        "type": "complete",
        "result": {
            "summary": "Ollama authored the repair source; guarded tools validated, tested, and committed it.",
            "evidence_count": len(observations),
            "tools_used": sorted({item.get("tool") for item in observations}),
            "model_authored_patch": True,
        },
        "summary": "Model-authored repair is committed and ready for independent review.",
        "model_mode": authored_mode,
    }


def _repair_model_context(case_context: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Bound the coding prompt to source and executable evidence instead of forwarding the full Workflow graph."""
    inspection = next(
        (item.get("result", {}) for item in reversed(observations) if item.get("tool") == "code.inspect"),
        {},
    )
    last_successful_patch = next(
        (
            item.get("arguments", {})
            for item in reversed(observations)
            if item.get("tool") == "code.patch" and item.get("result", {}).get("success") is True
        ),
        {},
    )
    last_patch_result = next(
        (item.get("result", {}) for item in reversed(observations) if item.get("tool") == "code.patch"),
        {},
    )
    latest_tests = next(
        (item.get("result", {}) for item in reversed(observations) if item.get("tool") == "tests.run"),
        case_context.get("failure", {}),
    )
    return {
        "incident": case_context.get("event", {}),
        "editable_file": "order_service.py",
        "current_order_service.py": last_successful_patch.get("replacement_content") or inspection.get("order_service.py", ""),
        "dependency_inventory_service.py": inspection.get("inventory_service.py", ""),
        "provided_payment_gateway.py": inspection.get("payment_gateway.py", ""),
        "tests_test_contract.py": inspection.get("tests/test_contract.py", ""),
        "side_effect_strategy": case_context.get("side_effect_strategy", "none"),
        "external_call_requirement": (
            "Import create_payment from payment_gateway. After inventory acceptance call it exactly once using "
            "SIGNALROOM_PAYMENT_CODE and SIGNALROOM_AMOUNT_CENTS from os.environ, and return its receipt under "
            "the checkout result's payment key. Never call it for a rejected checkout."
            if case_context.get("side_effect_strategy", "none") != "none" else
            "This run has no external API side effect; repair only the inventory compatibility defect."
        ),
        "runtime_log": inspection.get("runtime.log", ""),
        "latest_test_result": {
            "exit_code": latest_tests.get("exit_code"),
            "output": str(latest_tests.get("output", ""))[-8_000:],
        },
        "latest_patch_validation": last_patch_result if last_patch_result.get("success") is False else None,
    }


def model_turn(definition: dict[str, Any], case_context: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    if definition.get("requires_model"):
        controlled = _repair_control_command(definition, observations)
        if controlled is not None:
            return controlled
    fallback = _fallback(definition, observations)
    if os.getenv("SIGNALROOM_AGENT_MODEL", "ollama").lower() != "ollama":
        if definition.get("requires_model"):
            raise RuntimeError("Repair Agent requires Ollama to author source code; deterministic fallback is disabled")
        return fallback
    try:
        model_name = selected_ollama_model()
    except RuntimeError as error:
        if definition.get("requires_model"):
            raise RuntimeError(f"Repair Agent requires a local Ollama model: {error}") from error
        model_name = "unavailable"

    def attempted_fallback(reason: str) -> dict[str, Any]:
        if definition.get("requires_model"):
            raise RuntimeError(f"Repair Agent could not obtain a valid model-authored command: {reason}")
        return {
            **fallback,
            "model_mode": f"ollama/{model_name} → safe-fallback",
            "fallback_reason": reason,
        }

    completed = {item["tool"] for item in observations}
    remaining_required = [tool for tool in definition["required_tools"] if tool not in completed]
    schema = {
        "type": "call_tool | complete",
        "tool": "one allowed tool when type=call_tool",
        "arguments": "must follow the selected tool contract exactly",
        "summary": "concise evidence-based rationale, no hidden chain of thought",
        "result": "object when type=complete",
    }
    repair_generation = bool(definition.get("requires_model"))
    if repair_generation:
        prompt = {
            "task": "Diagnose the defect and author the complete corrected order_service.py source. Preserve backward compatibility, satisfy the selected external-call contract when present, and do not modify tests.",
            "evidence": _repair_model_context(case_context, observations),
            "response_schema": {
                "diagnosis": "concise defect diagnosis",
                "changed_file": "must equal order_service.py",
                "replacement_content": "complete syntactically valid Python source authored by you",
                "reasoning_summary": "concise evidence-based explanation without hidden chain of thought",
                "confidence": "number from 0 to 1",
            },
            "rules": [
                "Return JSON only",
                "Do not return a diff",
                "Do not modify tests",
                "Do not claim tests have passed",
                "Keep replacement_content focused and under 3000 characters",
                "Keep diagnosis and reasoning_summary under 80 words each",
            ],
        }
    else:
        prompt = {
            "goal": definition["purpose"],
            "case": case_context,
            "observations": observations,
            "available_tool_contracts": {tool: TOOL_CONTRACTS[tool] for tool in definition["tools"]},
            "required_tools_remaining": remaining_required,
            "response_schema": schema,
            "rules": [
                "Return JSON only",
                "Use only available tools and their exact argument contracts",
                "Complete only after required tools are used",
                "Do not claim evidence not present in observations",
                "Keep summary and result text under 80 words",
            ],
        }
    system_message = (
        "You are a senior software repair agent. Diagnose the supplied failure and author the smallest robust, "
        "backward-compatible fix. Return JSON only in the requested schema."
        if repair_generation else definition["system_prompt"]
    )
    request = urllib.request.Request(
        os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat"),
        data=json.dumps({
            "model": model_name, "stream": False, "think": ollama_think_setting(model_name), "format": "json",
            "messages": [{"role": "system", "content": system_message}, {"role": "user", "content": json.dumps(prompt)}],
            "options": {"temperature": 0.1, "num_predict": ollama_output_budget(repair_generation)},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _OLLAMA_LOCK:
            with urllib.request.urlopen(request, timeout=float(os.getenv("OLLAMA_TIMEOUT", "180"))) as response:
                raw = json.loads(response.read())
        content = raw.get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                "Ollama returned no final JSON content "
                f"(done_reason={raw.get('done_reason', 'unknown')}, "
                f"thinking_chars={len(str(raw.get('message', {}).get('thinking', '')))})"
            )
        model_output = parse_model_json(content)
        if repair_generation:
            command = {
                "type": "call_tool",
                "tool": "code.patch",
                "arguments": {
                    "file": model_output.get("changed_file"),
                    "replacement_content": model_output.get("replacement_content"),
                    "rationale": model_output.get("reasoning_summary") or model_output.get("diagnosis", ""),
                },
                "summary": model_output.get("diagnosis", "Ollama authored a source repair from the recorded evidence."),
                "result": {"confidence": model_output.get("confidence")},
            }
        else:
            command = model_output
        command["model_mode"] = f"ollama/{model_name}"
        error = _command_error(command, definition, observations)
        if error is None:
            return command
        return attempted_fallback(error)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError) as error:
        return attempted_fallback(f"{type(error).__name__}: {error}")
