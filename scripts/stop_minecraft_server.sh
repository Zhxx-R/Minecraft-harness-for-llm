#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT_DIR/infra/minecraft-server/server.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No server.pid found; the managed server may not be running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Sent SIGTERM to Minecraft server process $PID."
else
  echo "Process $PID is not running."
fi
rm -f "$PID_FILE"
