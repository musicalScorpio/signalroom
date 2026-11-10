#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if docker inspect signalroom-temporal >/dev/null 2>&1; then
  docker start signalroom-temporal >/dev/null 2>&1 || true
else
  docker run -d --name signalroom-temporal \
    -p 7233:7233 -p 8233:8233 \
    temporalio/temporal:latest server start-dev --ip 0.0.0.0 >/dev/null
fi

for _attempt in {1..30}; do
  if docker exec signalroom-temporal temporal operator namespace list >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker exec signalroom-temporal temporal operator namespace list >/dev/null 2>&1; then
  echo "Temporal did not become ready within 30 seconds." >&2
  exit 1
fi

uv sync

export SIGNALROOM_DB="${SIGNALROOM_DB:-$PROJECT_DIR/signalroom.db}"
export SIGNALROOM_ENABLE_CHAOS_DEMO="${SIGNALROOM_ENABLE_CHAOS_DEMO:-1}"

uv run python worker_supervisor.py &
SUPERVISOR_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run python server.py --port "${SIGNALROOM_PORT:-4173}"
