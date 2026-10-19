#!/usr/bin/env bash
# Start a complete local stack for end-to-end tests: fake llama-server, API (serving the
# built web app) and a separate worker process. Requires a migrated database.
#
#   DATABASE_URL=postgresql+asyncpg://... infra/scripts/e2e-stack.sh
#
# Writes PIDs to $E2E_STATE_DIR/pids; stop everything with `kill $(cat .../pids)`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STATE="${E2E_STATE_DIR:-$ROOT/.e2e}"
mkdir -p "$STATE"
: "${DATABASE_URL:?set DATABASE_URL}"

export APP_ENV=development
export MODEL_PROVIDER=llama_cpp
export MODEL_BASE_URL="http://127.0.0.1:${FAKE_LLAMA_PORT:-18080}/v1"
export MODEL_DEFAULT_ID=fake-model
export STATIC_ROOT="$ROOT/apps/web/dist"
export BOOTSTRAP_USERNAME="${E2E_USERNAME:-e2e}"
export BOOTSTRAP_PASSWORD="${E2E_PASSWORD:-e2e-password-123}"
export RUN_LEASE_SECONDS="${RUN_LEASE_SECONDS:-6}"

cd "$ROOT/services/api"
uv sync --frozen --quiet
# Run the venv's binaries directly (not via `uv run`) so the recorded PIDs are the real
# processes, and `kill -9 $(cat worker.pid)` really kills the worker.
PY="$ROOT/services/api/.venv/bin/python"
"$PY" -m devtools.fake_llama_server --port "${FAKE_LLAMA_PORT:-18080}" \
  >"$STATE/fake-llama.log" 2>&1 &
echo $! >"$STATE/pids"
"$PY" -m uvicorn app.main:app --timeout-graceful-shutdown 2 --port "${API_PORT:-18000}" >"$STATE/api.log" 2>&1 &
echo $! >>"$STATE/pids"
"$PY" -m app.worker >"$STATE/worker.log" 2>&1 &
echo $! >"$STATE/worker.pid"
cat "$STATE/worker.pid" >>"$STATE/pids"

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${API_PORT:-18000}/api/v1/health/ready" >/dev/null 2>&1; then
    echo "stack ready on http://127.0.0.1:${API_PORT:-18000}"
    exit 0
  fi
  sleep 0.2
done
echo "stack failed to start; see $STATE/*.log" >&2
exit 1
