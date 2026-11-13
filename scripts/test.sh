#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if ! ./.venv/bin/python -c "import playwright" >/dev/null 2>&1; then
  echo "Playwright is not installed. Run: uv pip install -r requirements-dev.txt" >&2
  exit 1
fi

echo "Running unit and integration tests..."
./.venv/bin/python -m unittest -v tests.test_agent_pipeline tests.test_signalroom tests.test_reliability_lab
./.venv/bin/python -m unittest discover -s tests/integration -t . -p 'test_*.py' -v

echo "Running Playwright browser tests against ${SIGNALROOM_BASE_URL:-http://127.0.0.1:4173}..."
./.venv/bin/python -m unittest discover -s tests/playwright -p 'test_*.py' -v
