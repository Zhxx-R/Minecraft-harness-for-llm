#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime/handoff"
DATABASE_PATH=""
BACKEND_PORT=8000
FRONTEND_PORT=5173

usage() {
  printf '%s\n' \
    "Usage: $0 [--database-path FILE] [--backend-port PORT] [--frontend-port PORT]" \
    "" \
    "Without --database-path, DATABASE_URL from .env is used."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --database-path) DATABASE_PATH="${2:?--database-path requires a value}"; shift ;;
    --backend-port) BACKEND_PORT="${2:?--backend-port requires a value}"; shift ;;
    --frontend-port) FRONTEND_PORT="${2:?--frontend-port requires a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$ROOT_DIR"
if [[ ! -x backend/.venv/bin/uvicorn || ! -d frontend/node_modules ]]; then
  echo "Dependencies are missing. Run ./scripts/handoff/bootstrap.sh first." >&2
  exit 1
fi
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [[ -n "$DATABASE_PATH" ]]; then
  if [[ "$DATABASE_PATH" != /* ]]; then
    DATABASE_PATH="$ROOT_DIR/$DATABASE_PATH"
  fi
  if [[ ! -f "$DATABASE_PATH" ]]; then
    echo "Audit database not found: $DATABASE_PATH" >&2
    exit 1
  fi
  export DATABASE_URL="sqlite+pysqlite:///$DATABASE_PATH"
fi

mkdir -p "$RUNTIME_DIR"
for pid_name in backend frontend; do
  pid_file="$RUNTIME_DIR/$pid_name.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$pid_name is already running with PID $(cat "$pid_file")." >&2
    exit 1
  fi
done

backend/.venv/bin/python scripts/handoff/launch_background.py \
  --cwd "$ROOT_DIR/backend" \
  --log "$RUNTIME_DIR/backend.log" \
  --pid-file "$RUNTIME_DIR/backend.pid" \
  -- .venv/bin/uvicorn mc_agent_harness.main:app \
  --host 127.0.0.1 \
  --port "$BACKEND_PORT" >/dev/null
VITE_API_BASE_URL="http://127.0.0.1:$BACKEND_PORT" \
  backend/.venv/bin/python scripts/handoff/launch_background.py \
  --cwd "$ROOT_DIR/frontend" \
  --log "$RUNTIME_DIR/frontend.log" \
  --pid-file "$RUNTIME_DIR/frontend.pid" \
  -- npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1 && \
     curl -fsS "http://127.0.0.1:$FRONTEND_PORT" >/dev/null 2>&1; then
    echo "Dashboard: http://127.0.0.1:$FRONTEND_PORT"
    echo "Backend:   http://127.0.0.1:$BACKEND_PORT"
    echo "Logs:      $RUNTIME_DIR"
    exit 0
  fi
  sleep 1
done

echo "Dashboard failed to become healthy. Inspect $RUNTIME_DIR/*.log" >&2
exit 1
