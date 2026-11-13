#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SIGNALROOM_PORT="${SIGNALROOM_PORT:-4173}"
THIRD_PARTY_PORT="${THIRD_PARTY_PORT:-4180}"
TEMPORAL_GRPC_PORT="${TEMPORAL_GRPC_PORT:-7233}"
TEMPORAL_UI_PORT="${TEMPORAL_UI_PORT:-8233}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

RUNTIME_DIR="$PROJECT_DIR/.signalroom/runtime"
LOG_DIR="$PROJECT_DIR/.signalroom/logs"
DATA_DIR="$PROJECT_DIR/.signalroom/data"
mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$DATA_DIR"

export SIGNALROOM_DB="${SIGNALROOM_DB:-$DATA_DIR/signalroom.db}"
export THIRD_PARTY_DB="${THIRD_PARTY_DB:-$DATA_DIR/third_party.db}"
export SIGNALROOM_ENABLE_CHAOS_DEMO="${SIGNALROOM_ENABLE_CHAOS_DEMO:-1}"
export SIGNALROOM_AGENT_MODEL="${SIGNALROOM_AGENT_MODEL:-ollama}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-127.0.0.1:$TEMPORAL_GRPC_PORT}"
export TEMPORAL_TASK_QUEUE="${TEMPORAL_TASK_QUEUE:-signalroom-events}"
export THIRD_PARTY_BASE_URL="${THIRD_PARTY_BASE_URL:-http://127.0.0.1:$THIRD_PARTY_PORT}"
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:$OLLAMA_PORT/api/chat}"

OLLAMA_PID=""
PROVIDER_PID=""
SUPERVISOR_PID=""
API_PID=""

log() {
  printf '\n\033[1;32m[SignalRoom]\033[0m %s\n' "$1"
}

fail() {
  printf '\n\033[1;31m[SignalRoom] ERROR:\033[0m %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found. $2"
}

port_pid() {
  lsof -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | sed -n '1p' || true
}

wait_for_url() {
  local url="$1"
  local attempts="${2:-60}"
  local delay="${3:-0.5}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if curl --max-time 1 -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

stop_owned_signalroom_server() {
  local pid
  local response
  pid="$(port_pid "$SIGNALROOM_PORT")"
  [[ -z "$pid" ]] && return 0

  response="$(curl --max-time 1 -fsS "http://127.0.0.1:$SIGNALROOM_PORT/api/runtime" 2>/dev/null || true)"
  if [[ "$response" != *'"instance_id": "signalroom-local"'* && "$response" != *'Temporal preserves Workflow history'* ]]; then
    fail "Port $SIGNALROOM_PORT belongs to another application (PID $pid). Set SIGNALROOM_PORT to a free port."
  fi

  log "Stopping the previous SignalRoom API on port $SIGNALROOM_PORT"
  kill "$pid" 2>/dev/null || true
  for _attempt in {1..50}; do
    [[ -z "$(port_pid "$SIGNALROOM_PORT")" ]] && return 0
    sleep 0.1
  done
  fail "The previous SignalRoom API did not release port $SIGNALROOM_PORT."
}

stop_stale_supervisor() {
  local state_file="$PROJECT_DIR/.signalroom/worker-state.json"
  local supervisor_pid
  local command_line
  [[ -f "$state_file" ]] || return 0
  supervisor_pid="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("supervisor_pid") or "")' "$state_file" 2>/dev/null || true)"
  [[ "$supervisor_pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$supervisor_pid" 2>/dev/null || return 0
  command_line="$(ps -p "$supervisor_pid" -o command= 2>/dev/null || true)"
  [[ "$command_line" == *"backend.runtime.supervisor"* ]] || return 0

  log "Stopping stale Worker supervisor PID $supervisor_pid"
  kill "$supervisor_pid" 2>/dev/null || true
  for _attempt in {1..50}; do
    kill -0 "$supervisor_pid" 2>/dev/null || return 0
    sleep 0.1
  done
  fail "The previous Worker supervisor did not stop."
}

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  log "Stopping SignalRoom processes started by this script"
  for pid in "$API_PID" "$SUPERVISOR_PID" "$PROVIDER_PID" "$OLLAMA_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$API_PID" "$SUPERVISOR_PID" "$PROVIDER_PID" "$OLLAMA_PID"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

require_command curl "Install curl and retry."
require_command lsof "Install lsof and retry."
require_command uv "Install uv: https://docs.astral.sh/uv/"
require_command docker "Install Docker Desktop or Colima."
require_command ollama "Install Ollama: https://ollama.com/"

stop_owned_signalroom_server

log "Ensuring the local container runtime is online"
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    colima start
  elif [[ -d /Applications/Docker.app ]]; then
    open -ga Docker
  else
    fail "Docker is installed but its daemon is offline. Start Docker Desktop or Colima."
  fi
  for _attempt in {1..90}; do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
fi
docker info >/dev/null 2>&1 || fail "The Docker daemon did not become ready."

log "Starting or reusing Temporal"
if docker inspect signalroom-temporal >/dev/null 2>&1; then
  docker start signalroom-temporal >/dev/null
else
  docker run -d --name signalroom-temporal \
    -p "$TEMPORAL_GRPC_PORT:7233" -p "$TEMPORAL_UI_PORT:8233" \
    temporalio/temporal:latest server start-dev --ip 0.0.0.0 >/dev/null
fi
for _attempt in {1..60}; do
  if docker exec signalroom-temporal temporal operator namespace list >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec signalroom-temporal temporal operator namespace list >/dev/null 2>&1 \
  || fail "Temporal did not become ready within 60 seconds."

log "Synchronizing the Python environment"
uv sync --frozen
PYTHON="$PROJECT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || fail "uv did not create the project virtual environment."

stop_stale_supervisor

log "Starting or reusing Ollama"
OLLAMA_BASE_URL="${OLLAMA_URL%/api/*}"
if ! curl --max-time 1 -fsS "$OLLAMA_BASE_URL/api/tags" >/dev/null 2>&1; then
  OLLAMA_HOST="127.0.0.1:$OLLAMA_PORT" ollama serve >"$LOG_DIR/ollama.log" 2>&1 &
  OLLAMA_PID=$!
  wait_for_url "$OLLAMA_BASE_URL/api/tags" 60 0.5 \
    || fail "Ollama did not become ready. See $LOG_DIR/ollama.log"
fi
MODEL_SUMMARY="$("$PYTHON" -c 'from backend.ollama_runtime import ollama_status; s=ollama_status(); assert s["model_available"], "No installed Ollama model was found"; print("{} ({})".format(s["model"], s["selection_source"]))' 2>&1)" \
  || fail "$MODEL_SUMMARY. Pull a model first, for example: ollama pull gpt-oss:20b"

log "Initializing SQLite schemas"
"$PYTHON" -c 'import os; from pathlib import Path; from backend.legacy.core import Store; from backend.storage.case_store import CaseStore; from backend.storage.reliability_store import ReliabilityStore; from third_party_service.store import PaymentStore; signalroom=Path(os.environ["SIGNALROOM_DB"]); provider=Path(os.environ["THIRD_PARTY_DB"]); Store(signalroom); CaseStore(signalroom); ReliabilityStore(signalroom); PaymentStore(provider); print(f"SignalRoom DB: {signalroom}\nProvider DB:   {provider}")'

log "Starting or reusing the demo third-party service"
PROVIDER_RESPONSE="$(curl --max-time 1 -fsS "$THIRD_PARTY_BASE_URL/health" 2>/dev/null || true)"
if [[ "$PROVIDER_RESPONSE" == *'signalroom-demo-third-party'* ]]; then
  printf 'Reusing %s\n' "$THIRD_PARTY_BASE_URL"
elif [[ -n "$(port_pid "$THIRD_PARTY_PORT")" ]]; then
  fail "Port $THIRD_PARTY_PORT is occupied by a service that is not SignalRoom's demo provider."
else
  "$PYTHON" -u -m third_party_service.server --port "$THIRD_PARTY_PORT" >"$LOG_DIR/third-party.log" 2>&1 &
  PROVIDER_PID=$!
  wait_for_url "$THIRD_PARTY_BASE_URL/health" 60 0.25 \
    || fail "The demo provider did not become ready. See $LOG_DIR/third-party.log"
fi

log "Starting the supervised Temporal Worker"
"$PYTHON" -u -m backend.runtime.supervisor >"$LOG_DIR/worker-supervisor.log" 2>&1 &
SUPERVISOR_PID=$!
WORKER_READY=""
for _attempt in {1..80}; do
  WORKER_READY="$("$PYTHON" -c 'import json; from pathlib import Path; p=Path(".signalroom/worker-state.json"); s=json.loads(p.read_text()) if p.exists() else {}; print("{}:{}:{}".format(s.get("status", ""), s.get("supervisor_pid", ""), s.get("worker_pid", "")))' 2>/dev/null || true)"
  [[ "$WORKER_READY" == "online:$SUPERVISOR_PID:"* ]] && break
  sleep 0.25
done
[[ "$WORKER_READY" == "online:$SUPERVISOR_PID:"* ]] \
  || fail "The Temporal Worker did not become ready. See $LOG_DIR/worker-supervisor.log"

log "Starting the SignalRoom API and control room"
"$PYTHON" -u -m backend.api.server --port "$SIGNALROOM_PORT" >"$LOG_DIR/signalroom-api.log" 2>&1 &
API_PID=$!
wait_for_url "http://127.0.0.1:$SIGNALROOM_PORT/api/runtime" 60 0.25 \
  || fail "SignalRoom did not become ready. See $LOG_DIR/signalroom-api.log"
RUNTIME_JSON="$(curl --max-time 3 -fsS "http://127.0.0.1:$SIGNALROOM_PORT/api/runtime")"
RUNTIME_SUMMARY="$(printf '%s' "$RUNTIME_JSON" | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); assert d.get("instance_id")=="signalroom-local", "unexpected API instance"; assert d.get("temporal_connected"), "Temporal bridge is offline"; assert d.get("worker",{}).get("status")=="online", "Worker is offline"; assert d.get("ollama",{}).get("model_available"), "Ollama model is unavailable"; assert d.get("third_party",{}).get("status")=="ok", "demo provider is offline"; print("Temporal + Worker + Ollama + provider verified")' 2>&1)" \
  || fail "Startup health verification failed: $RUNTIME_SUMMARY. See $LOG_DIR"

cat <<EOF

SignalRoom is ready.

  Control room:    http://127.0.0.1:$SIGNALROOM_PORT
  Temporal UI:     http://127.0.0.1:$TEMPORAL_UI_PORT
  Demo provider:   $THIRD_PARTY_BASE_URL/health
  Ollama model:    $MODEL_SUMMARY
  SignalRoom DB:   $SIGNALROOM_DB
  Provider DB:     $THIRD_PARTY_DB
  Logs:            $LOG_DIR
  Health:          $RUNTIME_SUMMARY

Press Ctrl+C to stop the API, Worker supervisor, and services started by this script.
The Temporal container is intentionally left running so durable history remains available.
EOF

wait "$API_PID"
