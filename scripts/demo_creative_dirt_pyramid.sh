#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TASK_ID="creative:24"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR="${DEMO_OUTPUT_DIR:-${ROOT_DIR}/runs/demos/creative_dirt_pyramid_${TIMESTAMP}}"
REVIEW_API_PORT="${DEMO_REVIEW_API_PORT:-8000}"
REVIEW_UI_PORT="${DEMO_REVIEW_UI_PORT:-5173}"
START_REVIEW_UI="${DEMO_START_REVIEW_UI:-1}"
OPEN_REVIEW_UI="${DEMO_OPEN_REVIEW_UI:-1}"
API_PID=""
UI_PID=""

cleanup_review_services() {
  local exit_code=$?
  if [[ -n "$UI_PID" ]] && kill -0 "$UI_PID" 2>/dev/null; then
    kill "$UI_PID" 2>/dev/null || true
    wait "$UI_PID" 2>/dev/null || true
  fi
  if [[ -n "$API_PID" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
  trap - EXIT INT TERM
  exit "$exit_code"
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local pid="$3"
  local attempts=0
  until curl --fail --silent --show-error "$url" >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '%s stopped before becoming ready: %s\n' "$label" "$url" >&2
      return 1
    fi
    if [[ "$attempts" -ge 60 ]]; then
      printf '%s did not become ready within 60 seconds: %s\n' "$label" "$url" >&2
      return 1
    fi
    sleep 1
  done
}

if [[ "$START_REVIEW_UI" != "0" ]] && [[ "$START_REVIEW_UI" != "1" ]]; then
  printf 'DEMO_START_REVIEW_UI must be 0 or 1.\n' >&2
  exit 1
fi
if [[ "$OPEN_REVIEW_UI" != "0" ]] && [[ "$OPEN_REVIEW_UI" != "1" ]]; then
  printf 'DEMO_OPEN_REVIEW_UI must be 0 or 1.\n' >&2
  exit 1
fi
if [[ -e "$OUTPUT_DIR" ]]; then
  printf 'Refusing to reuse an existing demo output directory: %s\n' "$OUTPUT_DIR" >&2
  printf 'Unset DEMO_OUTPUT_DIR or choose a new path.\n' >&2
  exit 1
fi

printf 'Creative demo task: %s — Build a dirt pyramid.\n' "$TASK_ID"
printf 'Online MineCLIP feedback is advisory; the final decision remains human review.\n\n'

scripts/run_week11_local_creative.sh \
  --task-id "$TASK_ID" \
  --output-dir "$OUTPUT_DIR" \
  --max-steps 80 \
  --max-runtime-sec 1200 \
  "$@"

backend/.venv/bin/python - "$OUTPUT_DIR" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
summary = json.loads((output_dir / "workflow_summary.json").read_text(encoding="utf-8"))
evaluation = json.loads(
    (output_dir / "evaluation" / "creative_evaluation.json").read_text(encoding="utf-8")
)
result = evaluation.get("result") or {}

print("\nCreative execution and MineCLIP evaluation complete")
print(f"  task:       {summary.get('task_id')}")
print(f"  run:        {summary.get('run_id')}")
print(f"  score:      {result.get('score')}")
print(f"  frames:     {result.get('frame_count')}")
print(f"  windows:    {result.get('window_count')}")
print(f"  agent POV:  {summary.get('recording')}")
print(f"  evaluation: {summary.get('evaluation_report')}")
print("  authority:  awaiting human review")
PY

if [[ "$START_REVIEW_UI" == "0" ]]; then
  printf '\nHuman review is ready in the audit database.\n'
  printf 'Start it later with:\n'
  printf '  scripts/start_week11_audit_backend.sh %q\n' "$OUTPUT_DIR"
  printf '  scripts/dev-frontend.sh\n'
  exit 0
fi

trap cleanup_review_services EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

APP_PORT="$REVIEW_API_PORT" \
  scripts/start_week11_audit_backend.sh "$OUTPUT_DIR" \
  >"${OUTPUT_DIR}/audit_backend.log" 2>&1 &
API_PID=$!

(
  cd frontend
  VITE_API_BASE_URL="http://127.0.0.1:${REVIEW_API_PORT}" \
    npm run dev -- --host 127.0.0.1 --port "$REVIEW_UI_PORT"
) >"${OUTPUT_DIR}/audit_frontend.log" 2>&1 &
UI_PID=$!

if ! wait_for_url "http://127.0.0.1:${REVIEW_API_PORT}/api/health" "Audit backend" "$API_PID"; then
  tail -n 40 "${OUTPUT_DIR}/audit_backend.log" >&2 || true
  exit 1
fi
if ! wait_for_url "http://127.0.0.1:${REVIEW_UI_PORT}/" "Audit frontend" "$UI_PID"; then
  tail -n 40 "${OUTPUT_DIR}/audit_frontend.log" >&2 || true
  exit 1
fi

REVIEW_URL="http://127.0.0.1:${REVIEW_UI_PORT}/#/creative"
printf '\nHuman-in-the-loop review is ready: %s\n' "$REVIEW_URL"
printf 'Review the MineCLIP score, trend, key frames, Agent POV video, and trajectory.\n'
printf 'Then choose Approve, Reject, Request Revision, or Inconclusive.\n'
printf 'Press Ctrl-C after the review to stop the local dashboard services.\n\n'

if [[ "$OPEN_REVIEW_UI" == "1" ]] && command -v open >/dev/null 2>&1; then
  open "$REVIEW_URL"
fi

while kill -0 "$API_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 1
done

printf 'A review service exited unexpectedly. Check:\n' >&2
printf '  %s\n' "${OUTPUT_DIR}/audit_backend.log" "${OUTPUT_DIR}/audit_frontend.log" >&2
exit 1
