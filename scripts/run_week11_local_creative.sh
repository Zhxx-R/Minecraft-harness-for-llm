#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

load_env_file() {
  local env_file="$1"
  local line key index
  local -a override_keys=()
  local -a override_values=()
  while IFS= read -r line; do
    if [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; then
      key="${line%%=*}"
      if declare -p "$key" >/dev/null 2>&1; then
        override_keys+=("$key")
        override_values+=("${!key}")
      fi
    fi
  done < "$env_file"
  set -a
  # shellcheck disable=SC1091
  source "$env_file"
  set +a
  for ((index = 0; index < ${#override_keys[@]}; index++)); do
    export "${override_keys[index]}=${override_values[index]}"
  done
}

if [[ -f .env ]]; then
  load_env_file .env
fi

MINECRAFT_HOST="${MINECRAFT_HOST:-127.0.0.1}"
MINECRAFT_PORT="${MINECRAFT_PORT:-25565}"
MINECRAFT_RCON_PORT="${MINECRAFT_RCON_PORT:-25575}"
MC_AGENT_RECORDING_WINDOW_TITLE="${MC_AGENT_RECORDING_WINDOW_TITLE:-Minecraft}"
MC_AGENT_SPECTATOR_WAIT_SEC="${MC_AGENT_SPECTATOR_WAIT_SEC:-300}"
MC_AGENT_STOP_SERVER_AFTER_RUN="${MC_AGENT_STOP_SERVER_AFTER_RUN:-1}"
export MC_SERVER_XMS="${MC_SERVER_XMS:-1G}"
export MC_SERVER_XMX="${MC_SERVER_XMX:-2500M}"
SERVER_PID_FILE="${ROOT_DIR}/infra/minecraft-server/server.pid"
SERVER_STARTED_BY_WORKFLOW=0

cleanup() {
  local exit_code=$?
  scripts/mineclip_scorer.sh stop >/dev/null 2>&1 || true
  if [[ "$SERVER_STARTED_BY_WORKFLOW" -eq 1 ]] && [[ "$MC_AGENT_STOP_SERVER_AFTER_RUN" == "1" ]]; then
    scripts/stop_minecraft_server.sh >/dev/null 2>&1 || true
  fi
  trap - EXIT INT TERM
  exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "${QWEN_API_KEY:-}" ]] || [[ "$QWEN_API_KEY" == "replace-me" ]]; then
  printf 'Set QWEN_API_KEY in the environment or project .env before running Week11.\n' >&2
  exit 1
fi
if [[ -z "${MINECRAFT_RCON_PASSWORD:-}" ]] || [[ "$MINECRAFT_RCON_PASSWORD" == "replace-me" ]]; then
  printf 'Set MINECRAFT_RCON_PASSWORD in the environment or project .env.\n' >&2
  exit 1
fi
if [[ -z "${MC_AGENT_SPECTATOR_PLAYER:-}" ]]; then
  printf 'Set MC_AGENT_SPECTATOR_PLAYER to the Minecraft client player used for agent POV capture.\n' >&2
  exit 1
fi
if [[ ! -x services/mineclip-scorer/.venv/bin/uvicorn ]] \
  || [[ ! -f services/mineclip-scorer/checkpoints/attn.pth ]]; then
  printf 'MineCLIP is not installed. Run: make mineclip-scorer-setup\n' >&2
  exit 1
fi
if [[ "$MC_AGENT_STOP_SERVER_AFTER_RUN" != "0" ]] && [[ "$MC_AGENT_STOP_SERVER_AFTER_RUN" != "1" ]]; then
  printf 'MC_AGENT_STOP_SERVER_AFTER_RUN must be 0 or 1.\n' >&2
  exit 1
fi

if [[ ! -f "$SERVER_PID_FILE" ]] || ! kill -0 "$(cat "$SERVER_PID_FILE")" 2>/dev/null; then
  SERVER_STARTED_BY_WORKFLOW=1
fi
scripts/start_minecraft_server.sh --background
backend/.venv/bin/python - \
  "$MINECRAFT_HOST" "$MINECRAFT_PORT" \
  "${MINECRAFT_RCON_HOST:-127.0.0.1}" "$MINECRAFT_RCON_PORT" <<'PY'
import socket
import sys
import time

endpoints = [
    (sys.argv[1], int(sys.argv[2]), "game"),
    (sys.argv[3], int(sys.argv[4]), "RCON"),
]
for host, port, label in endpoints:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Minecraft {label} endpoint is ready at {host}:{port}.")
                break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(
            f"Minecraft {label} endpoint did not become ready at {host}:{port} within 120 seconds."
        )
PY

printf 'Waiting up to %s seconds for spectator player %s to join the server...\n' \
  "$MC_AGENT_SPECTATOR_WAIT_SEC" "$MC_AGENT_SPECTATOR_PLAYER"
PYTHONPATH=backend/src backend/.venv/bin/python scripts/wait_for_minecraft_player.py \
  --host "${MINECRAFT_RCON_HOST:-127.0.0.1}" \
  --port "$MINECRAFT_RCON_PORT" \
  --player "$MC_AGENT_SPECTATOR_PLAYER" \
  --timeout-sec "$MC_AGENT_SPECTATOR_WAIT_SEC"

scripts/mineclip_scorer.sh start

PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week11_creative_task.py \
  --host "$MINECRAFT_HOST" \
  --port "$MINECRAFT_PORT" \
  --rcon-reset \
  --rcon-port "$MINECRAFT_RCON_PORT" \
  --rcon-password "$MINECRAFT_RCON_PASSWORD" \
  --random-teleport \
  --threat-pause \
  --spectator-player "$MC_AGENT_SPECTATOR_PLAYER" \
  --recording-window-title "$MC_AGENT_RECORDING_WINDOW_TITLE" \
  --max-steps 80 \
  --max-runtime-sec 1800 \
  --mineclip-progress-feedback \
  "$@"
