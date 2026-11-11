#!/usr/bin/env python3
"""Compatibility entry point for the Temporal Worker."""

import asyncio

from signalroom.runtime.worker import main


if __name__ == "__main__":
    asyncio.run(main())
