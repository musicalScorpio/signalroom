#!/usr/bin/env bash
set -euo pipefail

# Backward-compatible alias. The full local stack is owned by start.sh.
exec "$(cd "$(dirname "$0")" && pwd)/start.sh" "$@"
