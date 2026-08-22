#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-}"

if [[ -z "$OUTPUT_DIR" ]]; then
  LATEST_SUMMARY="$(find "${ROOT_DIR}/runs/week11" -name workflow_summary.json -type f -print 2>/dev/null \
    | sort | tail -1)"
  if [[ -z "$LATEST_SUMMARY" ]]; then
    printf 'No Week11 workflow summary was found under runs/week11.\n' >&2
    exit 1
  fi
  OUTPUT_DIR="$(dirname "$LATEST_SUMMARY")"
fi

OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
LIVE_REPORT="${OUTPUT_DIR}/live_training.json"
if [[ ! -f "$LIVE_REPORT" ]]; then
  printf 'Week11 live report not found: %s\n' "$LIVE_REPORT" >&2
  exit 1
fi

DATABASE_URL="$("${ROOT_DIR}/backend/.venv/bin/python" - "$LIVE_REPORT" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
database_url = payload.get("database_url")
if not isinstance(database_url, str) or not database_url:
    raise SystemExit("live_training.json does not contain database_url")
print(database_url)
PY
)"

export DATABASE_URL
export ARTIFACT_ROOT="${ROOT_DIR}/runs"
cd "${ROOT_DIR}/backend"
exec .venv/bin/uvicorn mc_agent_harness.main:app \
  --host "${APP_HOST:-127.0.0.1}" \
  --port "${APP_PORT:-8000}"
