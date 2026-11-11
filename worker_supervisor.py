#!/usr/bin/env python3
"""Compatibility entry point for the local Worker supervisor."""

from signalroom.runtime.supervisor import WorkerSupervisor


if __name__ == "__main__":
    WorkerSupervisor().run()
