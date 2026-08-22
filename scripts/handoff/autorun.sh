#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE="${1:-offline}"
if [[ $# -gt 0 ]]; then
  shift
fi
WITH_INFRA=false
ACCEPT_EULA=false
SKIP_TESTS=false
DEMO_ARGS=()

usage() {
  printf '%s\n' \
    "Usage: $0 [offline|live] [options]" \
    "" \
    "Options:" \
    "  --with-infra               Start PostgreSQL/pgvector and Redis." \
    "  --accept-minecraft-eula    Required for first-time live setup." \
    "  --skip-tests               Skip the full CI suite during bootstrap." \
    "  --task-id ID               Pass a task id to run_demo.sh." \
    "  --spectator-player NAME    Follow the bot from a connected client." \
    "  --no-threat-pause          Disable Carpet-based threat pausing." \
    "  --stop-server-after        Stop a server started by the live demo."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-infra) WITH_INFRA=true ;;
    --accept-minecraft-eula) ACCEPT_EULA=true ;;
    --skip-tests) SKIP_TESTS=true ;;
    --task-id|--spectator-player)
      DEMO_ARGS+=("$1" "${2:?$1 requires a value}")
      shift
      ;;
    --no-threat-pause|--stop-server-after) DEMO_ARGS+=("$1") ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if [[ "$MODE" != "offline" && "$MODE" != "live" ]]; then
  echo "Mode must be offline or live." >&2
  exit 2
fi

bootstrap_args=()
if "$WITH_INFRA"; then
  bootstrap_args+=(--with-infra)
fi
if "$SKIP_TESTS"; then
  bootstrap_args+=(--skip-tests)
fi
if [[ "$MODE" == "live" ]]; then
  bootstrap_args+=(--with-minecraft)
  if "$ACCEPT_EULA"; then
    bootstrap_args+=(--accept-minecraft-eula)
  fi
fi

cd "$ROOT_DIR"
./scripts/handoff/bootstrap.sh "${bootstrap_args[@]}"
./scripts/handoff/run_demo.sh "$MODE" "${DEMO_ARGS[@]}"
