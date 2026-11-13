"""Discover the active local Ollama model without assuming a model name."""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


def _matches(requested: str, candidate: str) -> bool:
    return requested == candidate or (":" not in requested and candidate.split(":", 1)[0] == requested)


def choose_model(configured: str, running: list[str], available: list[str]) -> tuple[str | None, str]:
    """Prefer a valid explicit choice, then a loaded model, then any installed model."""
    if configured:
        match = next((name for name in available if _matches(configured, name)), None)
        if match:
            return match, "configured"
    if running:
        return running[0], "running"
    if available:
        return available[0], "installed"
    return None, "unavailable"


def ollama_status() -> dict[str, Any]:
    endpoint = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    base = endpoint.rsplit("/api/", 1)[0]
    configured = os.getenv("OLLAMA_MODEL", "").strip()
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=0.8) as response:
            tags = json.loads(response.read())
        available = [item.get("name") for item in tags.get("models", []) if item.get("name")]
        try:
            with urllib.request.urlopen(f"{base}/api/ps", timeout=0.8) as response:
                processes = json.loads(response.read())
            running = [item.get("name") for item in processes.get("models", []) if item.get("name")]
        except Exception:
            running = []
        selected, source = choose_model(configured, running, available)
        return {
            "connected": True,
            "model": selected,
            "model_available": selected is not None,
            "selection_source": source,
            "requested_model": configured or None,
            "running": running,
            "available": available,
        }
    except Exception:
        return {
            "connected": False,
            "model": None,
            "model_available": False,
            "selection_source": "unavailable",
            "requested_model": configured or None,
            "running": [],
            "available": [],
        }


def selected_ollama_model() -> str:
    status = ollama_status()
    model = status.get("model")
    if not model:
        raise RuntimeError("No installed Ollama model was found")
    return str(model)

