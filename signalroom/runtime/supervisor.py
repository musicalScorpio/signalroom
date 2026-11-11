#!/usr/bin/env python3
"""Restart the local Temporal Worker while Temporal retains Workflow state."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from signalroom.runtime.state import WORKER_STATE_PATH, write_worker_state


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESTART_DELAY_SECONDS = float(os.getenv("SIGNALROOM_WORKER_RESTART_DELAY", "3.5"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkerSupervisor:
    def __init__(self) -> None:
        self.running = True
        self.child: subprocess.Popen[bytes] | None = None
        self.state: dict[str, Any] = {
            "status": "starting",
            "supervisor_pid": os.getpid(),
            "worker_pid": None,
            "restart_count": 0,
            "started_at": utc_now(),
            "last_exit_at": None,
            "last_restart_at": None,
            "last_exit_code": None,
        }

    def publish(self, **changes: Any) -> None:
        self.state.update(changes)
        write_worker_state(self.state, WORKER_STATE_PATH)

    def stop(self, _signum: int, _frame: Any) -> None:
        self.running = False
        self.publish(status="stopping")
        if self.child and self.child.poll() is None:
            self.child.terminate()

    def spawn(self) -> None:
        self.publish(status="starting")
        self.child = subprocess.Popen(
            [sys.executable, "-u", "-m", "signalroom.runtime.worker"],
            cwd=PROJECT_ROOT,
        )
        time.sleep(0.65)
        if self.child.poll() is None:
            self.publish(status="online", worker_pid=self.child.pid)

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        self.spawn()
        while self.running:
            assert self.child is not None
            exit_code = self.child.wait()
            if not self.running:
                break
            self.publish(
                status="restarting",
                worker_pid=None,
                last_exit_at=utc_now(),
                last_exit_code=exit_code,
            )
            deadline = time.monotonic() + RESTART_DELAY_SECONDS
            while self.running and time.monotonic() < deadline:
                time.sleep(0.1)
            if not self.running:
                break
            self.state["restart_count"] += 1
            self.publish(last_restart_at=utc_now())
            self.spawn()
        self.publish(status="stopped", worker_pid=None)


if __name__ == "__main__":
    WorkerSupervisor().run()
