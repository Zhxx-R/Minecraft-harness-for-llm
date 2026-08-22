#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${1:-offline}"
if [[ $# -gt 0 ]]; then
  shift
fi
TASK_ID="harvest_1_dirt"
SPECTATOR_PLAYER=""
STOP_SERVER_AFTER=false
NO_THREAT_PAUSE=false

usage() {
  printf '%s\n' \
    "Usage: $0 [offline|live] [options]" \
    "" \
    "Options:" \
    "  --task-id ID             Executable MineDojo task id (default: harvest_1_dirt)." \
    "  --spectator-player NAME  Make a connected client follow HarnessTrainer1." \
    "  --no-threat-pause        Do not use Carpet tick freeze around hostile observations." \
    "  --stop-server-after      Stop a server started by this script after the demo."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task-id) TASK_ID="${2:?--task-id requires a value}"; shift ;;
    --spectator-player) SPECTATOR_PLAYER="${2:?--spectator-player requires a value}"; shift ;;
    --no-threat-pause) NO_THREAT_PAUSE=true ;;
    --stop-server-after) STOP_SERVER_AFTER=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if [[ "$MODE" != "offline" && "$MODE" != "live" ]]; then
  echo "Mode must be offline or live." >&2
  usage >&2
  exit 2
fi

cd "$ROOT_DIR"
if [[ ! -x backend/.venv/bin/python || ! -d workers/mineflayer-worker/node_modules ]]; then
  echo "Dependencies are missing. Run ./scripts/handoff/bootstrap.sh first." >&2
  exit 1
fi

EXTERNAL_QWEN_API_KEY="${QWEN_API_KEY:-}"
EXTERNAL_RCON_PASSWORD="${MINECRAFT_RCON_PASSWORD:-}"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
[[ -n "$EXTERNAL_QWEN_API_KEY" ]] && QWEN_API_KEY="$EXTERNAL_QWEN_API_KEY"
[[ -n "$EXTERNAL_RCON_PASSWORD" ]] && MINECRAFT_RCON_PASSWORD="$EXTERNAL_RCON_PASSWORD"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR="$ROOT_DIR/runs/handoff_demo_$TIMESTAMP"
mkdir -p "$OUTPUT_DIR"
MANIFEST_PATH="$ROOT_DIR/tasks/executable/minedojo_programmatic_tasks.jsonl"
if [[ ! -s "$MANIFEST_PATH" ]]; then
  echo "Executable task snapshot is missing. Rerun bootstrap." >&2
  exit 1
fi

if [[ "$MODE" == "offline" ]]; then
  backend/.venv/bin/python scripts/validate_json_schemas.py | tee "$OUTPUT_DIR/schema_validation.log"
  PYTHONPATH=backend/src backend/.venv/bin/python scripts/dump_agent_prompt.py \
    --manifest-dir "$MANIFEST_PATH" \
    --task-id "$TASK_ID" \
    --pretty > "$OUTPUT_DIR/prompt.json"
  PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week6_benchmark.py \
    --manifest-dir tasks/manifests \
    --task-id minedojo_harvest_dirt \
    --output-dir "$OUTPUT_DIR/benchmark" | tee "$OUTPUT_DIR/benchmark_summary.json"
  echo "Offline demo passed. Artifacts: $OUTPUT_DIR"
  exit 0
fi

if [[ -z "${QWEN_API_KEY:-}" || "${QWEN_API_KEY:-}" == "replace-me" ]]; then
  echo "Set QWEN_API_KEY in .env or export it before running the live demo." >&2
  exit 1
fi
if [[ -z "${MINECRAFT_RCON_PASSWORD:-}" || "${MINECRAFT_RCON_PASSWORD:-}" == "replace-me" ]]; then
  echo "MINECRAFT_RCON_PASSWORD is missing. Rerun bootstrap or set it in .env." >&2
  exit 1
fi
if [[ ! -f infra/minecraft-server/server-1.20.1.jar ]]; then
  echo "Minecraft server is missing. Run bootstrap with --with-minecraft --accept-minecraft-eula." >&2
  exit 1
fi

SERVER_WAS_STARTED=false
if ! python3 - "${MINECRAFT_HOST:-localhost}" "${MINECRAFT_PORT:-25565}" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
PY
then
  scripts/start_minecraft_server.sh --background
  SERVER_WAS_STARTED=true
fi

server_ready=false
for _ in $(seq 1 90); do
  if python3 - "${MINECRAFT_HOST:-localhost}" "${MINECRAFT_PORT:-25565}" <<'PY'
import socket
import sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1):
        pass
except OSError:
    raise SystemExit(1)
PY
  then
    server_ready=true
    break
  fi
  if [[ -f infra/minecraft-server/server.pid ]] && ! kill -0 "$(cat infra/minecraft-server/server.pid)" 2>/dev/null; then
    echo "Minecraft server exited during startup. See infra/minecraft-server/server.log" >&2
    exit 1
  fi
  sleep 1
done
if ! "$server_ready"; then
  echo "Minecraft server did not become ready within 90 seconds." >&2
  exit 1
fi
sleep 2

PYTHONPATH=backend/src backend/.venv/bin/python scripts/verify_llm_model.py \
  --output "$OUTPUT_DIR/model_verification.json" | tee "$OUTPUT_DIR/model_verification.summary.json"

live_args=(
  scripts/run_week10_live_training.py
  --manifest-dir "$MANIFEST_PATH"
  --task-id "$TASK_ID"
  --host "${MINECRAFT_HOST:-localhost}"
  --port "${MINECRAFT_PORT:-25565}"
  --worker-concurrency 1
  --max-steps-per-task 15
  --max-runtime-sec-per-task 360
  --start-delay-sec 3
  --clear-all-inventory-on-reset
  --rcon-reset
  --rcon-host "${MINECRAFT_RCON_HOST:-localhost}"
  --rcon-port "${MINECRAFT_RCON_PORT:-25575}"
  --rcon-set-time day
  --rcon-set-weather clear
  --rcon-random-teleport-on-reset
  --rcon-random-teleport-max-range 500
  --model-timeout-retries 2
  --model-timeout-requeues 1
  --auto-promote
  --output "$OUTPUT_DIR/live_demo.json"
  --database-path "$OUTPUT_DIR/live_demo.sqlite3"
)
if ! "$NO_THREAT_PAUSE" && [[ -f infra/minecraft-server/mods/fabric-carpet-1.20-1.4.112+v230608.jar ]]; then
  live_args+=(--threat-pause)
fi
if [[ -n "$SPECTATOR_PLAYER" ]]; then
  live_args+=(--spectator-player "$SPECTATOR_PLAYER")
fi

set +e
PYTHONPATH=backend/src backend/.venv/bin/python "${live_args[@]}" 2>&1 | tee "$OUTPUT_DIR/live_demo.log"
LIVE_RC=${PIPESTATUS[0]}
set -e

if "$STOP_SERVER_AFTER" && "$SERVER_WAS_STARTED"; then
  scripts/stop_minecraft_server.sh
fi

cat <<EOF

Live demo finished with exit code $LIVE_RC.
Artifacts: $OUTPUT_DIR
Audit dashboard:
  ./scripts/handoff/start_dashboard.sh --database-path "$OUTPUT_DIR/live_demo.sqlite3"
EOF
exit "$LIVE_RC"
