#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${1:-plan}"
if [[ $# -gt 0 ]]; then
  shift
fi

WORKER_COUNT="${WEEK10_WORKER_COUNT:-5}"
TASK_COUNT="${WEEK10_TASK_COUNT:-1581}"
SERVER_HEAP_GB="${WEEK10_SERVER_HEAP_GB:-3.0}"
MAX_TASK_RETRIES="${WEEK10_MAX_TASK_RETRIES:-5}"
MAX_TASK_SIMILARITY="${WEEK10_MAX_TASK_SIMILARITY:-1.0}"
RUN_LABEL="${WEEK10_RUN_LABEL:-week10-full}"

usage() {
  printf '%s\n' \
    "Usage: $0 plan [OUTPUT_DIR]" \
    "       $0 run [OUTPUT_DIR]" \
    "       $0 resume OUTPUT_DIR" \
    "" \
    "Environment overrides:" \
    "  WEEK10_WORKER_COUNT       default: 5" \
    "  WEEK10_TASK_COUNT         default: 1581" \
    "  WEEK10_SERVER_HEAP_GB     default: 3.0" \
    "  WEEK10_MAX_TASK_RETRIES   default: 5" \
    "  WEEK10_MAX_TASK_SIMILARITY default: 1.0 (keep five workers filled)"
}

case "$MODE" in
  plan|run|resume) ;;
  -h|--help) usage; exit 0 ;;
  *) echo "Mode must be plan, run, or resume." >&2; usage >&2; exit 2 ;;
esac

cd "$ROOT_DIR"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ "$MODE" == "resume" ]]; then
  if [[ $# -ne 1 ]]; then
    echo "resume requires the original output directory." >&2
    usage >&2
    exit 2
  fi
  OUTPUT_DIR="$1"
else
  OUTPUT_DIR="${1:-runs/formal/${RUN_LABEL}-${WORKER_COUNT}w-${timestamp}}"
fi

FORMAL_ARGS=(
  --task-count "$TASK_COUNT"
  --worker-concurrency "$WORKER_COUNT"
  --max-task-retries "$MAX_TASK_RETRIES"
  --max-task-similarity "$MAX_TASK_SIMILARITY"
  --include-survival
  --output-dir "$OUTPUT_DIR"
)

if [[ "$MODE" == "plan" ]]; then
  exec env PYTHONPATH=backend/src backend/.venv/bin/python \
    scripts/run_week10_formal_batch.py --dry-run "${FORMAL_ARGS[@]}"
fi

if [[ ! -f .env ]]; then
  echo "Missing .env. Run scripts/handoff/bootstrap.sh first." >&2
  exit 1
fi
if [[ ! -x backend/.venv/bin/python ]]; then
  echo "Missing backend virtual environment. Run scripts/handoff/bootstrap.sh first." >&2
  exit 1
fi
if [[ ! -f infra/minecraft-server/server-1.20.1.jar ]]; then
  echo "Minecraft server is not prepared." >&2
  echo "Run scripts/handoff/setup_minecraft_server.sh --accept-eula first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${QWEN_API_KEY:-}" || "${QWEN_API_KEY}" == "replace-me" ]]; then
  echo "Set QWEN_API_KEY in .env before live training." >&2
  exit 1
fi
if [[ -z "${MINECRAFT_RCON_PASSWORD:-}" || "${MINECRAFT_RCON_PASSWORD}" == "replace-me" ]]; then
  echo "Set MINECRAFT_RCON_PASSWORD in .env before live training." >&2
  exit 1
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "Set DATABASE_URL in .env; five-worker formal training requires PostgreSQL." >&2
  exit 1
fi

for command_name in docker java node; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
done

docker compose up -d postgres redis
for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U mc_agent -d mc_agent >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker compose exec -T postgres pg_isready -U mc_agent -d mc_agent >/dev/null
PYTHONPATH=backend/src backend/.venv/bin/python -m alembic upgrade head

PYTHONPATH=backend/src backend/.venv/bin/python \
  scripts/start_minecraft_server_pool.py \
  --server-count "$WORKER_COUNT" \
  --heap-gb "$SERVER_HEAP_GB"

mkdir -p .runtime "$OUTPUT_DIR"
printf '%s\n' "$OUTPUT_DIR" > .runtime/week10_full_5.latest

if [[ "$MODE" == "resume" ]]; then
  FORMAL_ARGS+=(--resume)
fi

exec env PYTHONPATH=backend/src backend/.venv/bin/python \
  scripts/run_week10_formal_batch.py "${FORMAL_ARGS[@]}"
