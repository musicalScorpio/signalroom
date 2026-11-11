"""Typed local Ollama agent harness with a deterministic safe fallback."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFINITIONS_PATH = Path(__file__).with_name("definitions.json")


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


def ollama_status() -> dict[str, Any]:
    endpoint = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    tags = endpoint.rsplit("/api/", 1)[0] + "/api/tags"
    try:
        with urllib.request.urlopen(tags, timeout=0.8) as response:
            payload = json.loads(response.read())
        configured = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        available = [item.get("name") for item in payload.get("models", [])]
        model_available = configured in available or any(name and name.split(":", 1)[0] == configured for name in available)
        return {"connected": True, "model": configured, "model_available": model_available, "available": available}
    except Exception:
        return {"connected": False, "model": os.getenv("OLLAMA_MODEL", "qwen3:4b"), "model_available": False, "available": []}


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


def model_turn(definition: dict[str, Any], case_context: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    fallback = _fallback(definition, observations)
    if os.getenv("SIGNALROOM_AGENT_MODEL", "ollama").lower() != "ollama":
        return fallback
    model_name = os.getenv("OLLAMA_MODEL", "qwen3:4b")

    def attempted_fallback(reason: str) -> dict[str, Any]:
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
        "arguments": {},
        "summary": "concise evidence-based rationale, no hidden chain of thought",
        "result": "object when type=complete",
    }
    prompt = {
        "goal": definition["purpose"],
        "case": case_context,
        "observations": observations,
        "allowed_tools": definition["tools"],
        "required_tools_remaining": remaining_required,
        "response_schema": schema,
        "rules": ["Return JSON only", "Use only allowed tools", "Complete only after required tools are used", "Do not claim evidence not present in observations"],
    }
    request = urllib.request.Request(
        os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat"),
        data=json.dumps({
            "model": model_name, "stream": False, "format": "json",
            "messages": [{"role": "system", "content": definition["system_prompt"]}, {"role": "user", "content": json.dumps(prompt)}],
            "options": {"temperature": 0.1},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=float(os.getenv("OLLAMA_TIMEOUT", "45"))) as response:
            raw = json.loads(response.read())
        command = json.loads(raw.get("message", {}).get("content", "{}"))
        command["model_mode"] = f"ollama/{model_name}"
        if command.get("type") == "call_tool" and command.get("tool") in definition["tools"] and command["tool"] not in completed:
            return command
        if command.get("type") == "complete" and not remaining_required and isinstance(command.get("result", {}), dict):
            return command
        return attempted_fallback("Model response did not satisfy the tool and evidence contract.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError) as error:
        return attempted_fallback(f"{type(error).__name__}: {error}")
