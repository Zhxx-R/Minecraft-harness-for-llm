#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime/handoff"
STOP_MINECRAFT=false
STOP_DOCKER=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minecraft) STOP_MINECRAFT=true ;;
    --docker) STOP_DOCKER=true ;;
    -h|--help)
      echo "Usage: $0 [--minecraft] [--docker]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

for service in backend frontend; do
  pid_file="$RUNTIME_DIR/$service.pid"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      echo "Stopped $service process $pid."
    fi
    rm -f "$pid_file"
  fi
done
if "$STOP_MINECRAFT"; then
  "$ROOT_DIR/scripts/stop_minecraft_server.sh"
fi
if "$STOP_DOCKER"; then
  (cd "$ROOT_DIR" && docker compose down)
fi
