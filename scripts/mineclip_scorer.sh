#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="${ROOT_DIR}/services/mineclip-scorer"
VENV_DIR="${SERVICE_DIR}/.venv"
LOCAL_ENV="${SERVICE_DIR}/.env.local"
RUNTIME_DIR="${ROOT_DIR}/.runtime/mineclip-scorer"
PID_FILE="${RUNTIME_DIR}/scorer.pid"
LOG_FILE="${RUNTIME_DIR}/scorer.log"

if [[ -f "$LOCAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi

export MINECLIP_VARIANT="${MINECLIP_VARIANT:-attn}"
export MINECLIP_CHECKPOINT="${MINECLIP_CHECKPOINT:-${SERVICE_DIR}/checkpoints/${MINECLIP_VARIANT}.pth}"
export MINECLIP_REPOSITORY="${MINECLIP_REPOSITORY:-${SERVICE_DIR}/vendor/MineCLIP}"
export MINECLIP_DEVICE="${MINECLIP_DEVICE:-auto}"
export HF_HOME="${HF_HOME:-${SERVICE_DIR}/cache/huggingface}"
HOST="${MINECLIP_SCORER_HOST:-127.0.0.1}"
PORT="${MINECLIP_SCORER_PORT:-8091}"
BASE_URL="http://${HOST}:${PORT}"

usage() {
  printf 'Usage: scripts/mineclip_scorer.sh {start|foreground|status|smoke|logs|stop}\n'
}

is_pid_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

is_ready() {
  curl --silent --fail --max-time 3 "${BASE_URL}/health" 2>/dev/null \
    | "${VENV_DIR}/bin/python" -c \
      'import json,sys; raise SystemExit(json.load(sys.stdin).get("status") != "ready")' \
    2>/dev/null
}

require_installation() {
  if [[ ! -x "${VENV_DIR}/bin/uvicorn" ]]; then
    printf 'MineCLIP environment is missing. Run: make mineclip-scorer-setup\n' >&2
    exit 1
  fi
  if [[ ! -f "$MINECLIP_CHECKPOINT" ]]; then
    printf 'MineCLIP checkpoint is missing: %s\n' "$MINECLIP_CHECKPOINT" >&2
    exit 1
  fi
  if [[ ! -d "$MINECLIP_REPOSITORY/mineclip" ]]; then
    printf 'MineCLIP repository is missing: %s\n' "$MINECLIP_REPOSITORY" >&2
    exit 1
  fi
}

start_scorer() {
  require_installation
  mkdir -p "$RUNTIME_DIR"
  if is_ready; then
    printf 'MineCLIP scorer is already ready at %s.\n' "$BASE_URL"
    return
  fi
  if is_pid_running; then
    printf 'MineCLIP scorer PID %s is running but not ready. See %s.\n' \
      "$(cat "$PID_FILE")" "$LOG_FILE" >&2
    exit 1
  fi
  rm -f "$PID_FILE"
  : > "$LOG_FILE"
  "${VENV_DIR}/bin/python" - \
    "${VENV_DIR}/bin/uvicorn" "$SERVICE_DIR" "$HOST" "$PORT" "$LOG_FILE" "$PID_FILE" <<'PY'
import subprocess
import sys

uvicorn, service_dir, host, port, log_file, pid_file = sys.argv[1:]
log_handle = open(log_file, "ab", buffering=0)
process = subprocess.Popen(
    [uvicorn, "app:app", "--app-dir", service_dir, "--host", host, "--port", port],
    stdin=subprocess.DEVNULL,
    stdout=log_handle,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
with open(pid_file, "w", encoding="utf-8") as stream:
    stream.write(f"{process.pid}\n")
PY
  for _ in $(seq 1 120); do
    if is_ready; then
      printf 'MineCLIP scorer ready at %s (PID %s).\n' "$BASE_URL" "$(cat "$PID_FILE")"
      return
    fi
    if ! is_pid_running; then
      printf 'MineCLIP scorer exited during startup. Last log lines:\n' >&2
      tail -40 "$LOG_FILE" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    sleep 1
  done
  printf 'MineCLIP scorer did not become ready within 120 seconds. See %s.\n' "$LOG_FILE" >&2
  stop_scorer
  exit 1
}

stop_scorer() {
  if ! is_pid_running; then
    rm -f "$PID_FILE"
    printf 'MineCLIP scorer is not running under project process management.\n'
    return
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid"
  for _ in $(seq 1 30); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      printf 'MineCLIP scorer stopped (PID %s).\n' "$pid"
      return
    fi
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  printf 'MineCLIP scorer was force-stopped after 30 seconds (PID %s).\n' "$pid"
}

case "${1:-}" in
  start)
    start_scorer
    ;;
  foreground)
    require_installation
    exec "${VENV_DIR}/bin/uvicorn" app:app \
      --app-dir "$SERVICE_DIR" \
      --host "$HOST" \
      --port "$PORT"
    ;;
  status)
    if is_ready; then
      curl --silent --fail "${BASE_URL}/health"
      printf '\n'
    else
      printf 'MineCLIP scorer is not ready at %s.\n' "$BASE_URL" >&2
      exit 1
    fi
    ;;
  smoke)
    if ! is_ready; then
      printf 'Start the scorer before running smoke: scripts/mineclip_scorer.sh start\n' >&2
      exit 1
    fi
    "${VENV_DIR}/bin/python" "${ROOT_DIR}/scripts/smoke_mineclip_scorer.py" \
      --scorer-url "$BASE_URL"
    ;;
  logs)
    touch "$LOG_FILE"
    tail -f "$LOG_FILE"
    ;;
  stop)
    stop_scorer
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
