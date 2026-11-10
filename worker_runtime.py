"""Shared state for the local Worker supervisor and control-room UI."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = PROJECT_ROOT / ".signalroom"
WORKER_STATE_PATH = RUNTIME_DIR / "worker-state.json"


def read_worker_state(path: Path = WORKER_STATE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "status": "unmanaged",
            "worker_pid": None,
            "supervisor_pid": None,
            "restart_count": 0,
        }


def write_worker_state(state: dict[str, Any], path: Path = WORKER_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="worker-state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def safe_worker_pid(state: dict[str, Any], server_pid: int | None = None) -> int | None:
    pid = state.get("worker_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    if server_pid is not None and pid == server_pid:
        return None
    return pid

