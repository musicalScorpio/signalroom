#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
PORT="${SIGNALROOM_PORT:-4173}"

# A previous fallback server can survive after its Worker has stopped and keep
# the demo port occupied. Replace only a process that identifies as SignalRoom;
# never terminate an unrelated application using the same port.
PORT_PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sed -n '1p' || true)"
if [[ -n "$PORT_PID" ]]; then
  RUNTIME_RESPONSE="$(curl --max-time 1 -fsS "http://127.0.0.1:$PORT/api/runtime" 2>/dev/null || true)"
  if [[ "$RUNTIME_RESPONSE" == *'"instance_id": "signalroom-local"'* || "$RUNTIME_RESPONSE" == *'Temporal preserves Workflow history'* ]]; then
    echo "Replacing stale SignalRoom process $PORT_PID on port $PORT..."
    kill "$PORT_PID" 2>/dev/null || true
    for _attempt in {1..30}; do
      if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
  else
    echo "Port $PORT is used by another application (PID $PORT_PID)." >&2
    echo "Stop that application or run SIGNALROOM_PORT=4174 ./scripts/dev.sh" >&2
    exit 1
  fi
fi

if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "SignalRoom could not release port $PORT. Stop PID $PORT_PID and retry." >&2
  exit 1
fi

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
export SIGNALROOM_AGENT_MODEL="${SIGNALROOM_AGENT_MODEL:-ollama}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:4b}"

if curl --max-time 1 -fsS "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; then
  echo "Ollama detected. Agents will request structured turns from $OLLAMA_MODEL."
else
  echo "Ollama is offline. Agents will use the same definitions with the deterministic safe policy."
fi

uv run python -m signalroom.runtime.supervisor &
SUPERVISOR_PID=$!

cleanup() {
  kill "$SUPERVISOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run python -m signalroom.api.server --port "$PORT"
