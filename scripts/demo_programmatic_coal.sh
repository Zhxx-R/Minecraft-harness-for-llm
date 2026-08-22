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

TASK_ID="${DEMO_PROGRAMMATIC_TASK_ID:-harvest_8_coal_with_iron_pickaxe}"
DEMO_SLUG="${DEMO_PROGRAMMATIC_SLUG:-coal}"
DEMO_MODE="${DEMO_PROGRAMMATIC_MODE:-ab}"
SKILL_BUNDLE="${DEMO_SKILL_BUNDLE:-${ROOT_DIR}/runs/exports/week10-24h-learned-skills.json}"
MINECRAFT_HOST="${MINECRAFT_HOST:-127.0.0.1}"
MINECRAFT_PORT="${MINECRAFT_PORT:-25565}"
MINECRAFT_RCON_PORT="${MINECRAFT_RCON_PORT:-25575}"
MC_AGENT_RECORDING_WINDOW_TITLE="${MC_AGENT_RECORDING_WINDOW_TITLE:-Minecraft}"
MC_AGENT_RECORDING_WINDOW_SCALE="${MC_AGENT_RECORDING_WINDOW_SCALE:-2.0}"
MC_AGENT_SPECTATOR_WAIT_SEC="${MC_AGENT_SPECTATOR_WAIT_SEC:-300}"
MC_AGENT_RECORDING_PREFLIGHT_DELAY_SEC="${MC_AGENT_RECORDING_PREFLIGHT_DELAY_SEC:-8}"
MC_AGENT_TRUSTED_WINDOW_PREFLIGHT="${MC_AGENT_TRUSTED_WINDOW_PREFLIGHT:-1}"
MC_AGENT_STOP_SERVER_AFTER_RUN="${MC_AGENT_STOP_SERVER_AFTER_RUN:-1}"
export MC_SERVER_XMS="${MC_SERVER_XMS:-1G}"
export MC_SERVER_XMX="${MC_SERVER_XMX:-2500M}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR="${DEMO_OUTPUT_DIR:-${ROOT_DIR}/runs/demos/programmatic_${DEMO_SLUG}_${TIMESTAMP}}"
MANIFEST_PATH="${DEMO_PROGRAMMATIC_MANIFEST_PATH:-${ROOT_DIR}/tasks/executable/minedojo_programmatic_tasks.jsonl}"
SERVER_PID_FILE="${ROOT_DIR}/infra/minecraft-server/server.pid"
SERVER_STARTED_BY_WORKFLOW=0

cleanup() {
  local exit_code=$?
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
  printf 'Set QWEN_API_KEY in the environment or project .env before running the demo.\n' >&2
  exit 1
fi
if [[ -z "${MINECRAFT_RCON_PASSWORD:-}" ]] || [[ "$MINECRAFT_RCON_PASSWORD" == "replace-me" ]]; then
  printf 'Set MINECRAFT_RCON_PASSWORD in the environment or project .env.\n' >&2
  exit 1
fi
if [[ -z "${MC_AGENT_SPECTATOR_PLAYER:-}" ]]; then
  printf 'Set MC_AGENT_SPECTATOR_PLAYER to the visible Minecraft client used for agent POV capture.\n' >&2
  exit 1
fi
if [[ "$MC_AGENT_STOP_SERVER_AFTER_RUN" != "0" ]] && [[ "$MC_AGENT_STOP_SERVER_AFTER_RUN" != "1" ]]; then
  printf 'MC_AGENT_STOP_SERVER_AFTER_RUN must be 0 or 1.\n' >&2
  exit 1
fi
if [[ "$MC_AGENT_TRUSTED_WINDOW_PREFLIGHT" != "0" ]] && [[ "$MC_AGENT_TRUSTED_WINDOW_PREFLIGHT" != "1" ]]; then
  printf 'MC_AGENT_TRUSTED_WINDOW_PREFLIGHT must be 0 or 1.\n' >&2
  exit 1
fi
if [[ "$DEMO_MODE" != "ab" ]] && [[ "$DEMO_MODE" != "no-skill" ]] && [[ "$DEMO_MODE" != "with-skill" ]]; then
  printf 'DEMO_PROGRAMMATIC_MODE must be ab, no-skill, or with-skill.\n' >&2
  exit 1
fi
if [[ ! -x backend/.venv/bin/python ]]; then
  printf 'Backend environment is missing: backend/.venv/bin/python\n' >&2
  exit 1
fi
if [[ ! -f "$SKILL_BUNDLE" ]]; then
  printf 'Frozen skill bundle is missing: %s\n' "$SKILL_BUNDLE" >&2
  exit 1
fi
if [[ -e "$OUTPUT_DIR" ]]; then
  printf 'Refusing to reuse an existing demo output directory: %s\n' "$OUTPUT_DIR" >&2
  printf 'Unset DEMO_OUTPUT_DIR or choose a new path.\n' >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

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

printf 'Join the server as %s. Waiting up to %s seconds before agent-POV recording starts...\n' \
  "$MC_AGENT_SPECTATOR_PLAYER" "$MC_AGENT_SPECTATOR_WAIT_SEC"
PYTHONPATH=backend/src backend/.venv/bin/python scripts/wait_for_minecraft_player.py \
  --host "${MINECRAFT_RCON_HOST:-127.0.0.1}" \
  --port "$MINECRAFT_RCON_PORT" \
  --player "$MC_AGENT_SPECTATOR_PLAYER" \
  --timeout-sec "$MC_AGENT_SPECTATOR_WAIT_SEC"

if [[ "$MC_AGENT_RECORDING_PREFLIGHT_DELAY_SEC" != "0" ]]; then
  printf 'Waiting %s seconds for the Minecraft OpenGL window to stabilize before capture...\n' \
    "$MC_AGENT_RECORDING_PREFLIGHT_DELAY_SEC"
  sleep "$MC_AGENT_RECORDING_PREFLIGHT_DELAY_SEC"
fi

if [[ "$MC_AGENT_TRUSTED_WINDOW_PREFLIGHT" == "0" ]] && [[ -z "${MC_AGENT_RECORDING_FILTER:-}" ]]; then
  MC_AGENT_RECORDING_FILTER="$(
    PYTHONPATH=backend/src backend/.venv/bin/python -c \
      'import sys; from mc_agent_harness.runtime.macos_window_capture import crop_filter_for_window, select_macos_window; print(crop_filter_for_window(select_macos_window(title=sys.argv[1]), float(sys.argv[2])))' \
      "$MC_AGENT_RECORDING_WINDOW_TITLE" "$MC_AGENT_RECORDING_WINDOW_SCALE"
  )"
  export MC_AGENT_RECORDING_FILTER
  printf 'Using window bounds without static screenshot preflight: %s\n' \
    "$MC_AGENT_RECORDING_FILTER"
fi

run_condition() {
  local condition="$1"
  local max_retrieved_skills="$2"
  shift 2
  local -a visual_capture_args=()
  local condition_dir="${OUTPUT_DIR}/${condition}"
  local live_report="${condition_dir}/live_training.json"
  local video_path="${condition_dir}/agent_pov.mp4"
  local database_path="${condition_dir}/audit.sqlite3"
  local database_url="sqlite+pysqlite:///${database_path}"

  mkdir -p "$condition_dir"

  if [[ "$MC_AGENT_TRUSTED_WINDOW_PREFLIGHT" == "1" ]]; then
    visual_capture_args=(
      --recording-window-title "$MC_AGENT_RECORDING_WINDOW_TITLE"
      --agent-visual-snapshots
      --initial-visual-snapshot
    )
  else
    visual_capture_args=(
      --recording-window-title ""
      --no-agent-visual-snapshots
      --no-initial-visual-snapshot
    )
  fi

  PYTHONPATH=backend/src backend/.venv/bin/python - \
    "$database_path" "$SKILL_BUNDLE" <<'PY'
import sys
from pathlib import Path

from mc_agent_harness.db.models import Base
from mc_agent_harness.db.session import create_database_engine, create_session_factory
from mc_agent_harness.skills.bundle import import_skill_bundle

database_path = Path(sys.argv[1]).resolve()
bundle_path = Path(sys.argv[2]).resolve()
engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
Base.metadata.create_all(engine)
result = import_skill_bundle(
    create_session_factory(engine),
    bundle_path,
    on_conflict="skip",
)
print(
    f"Skill library prepared: {result.created} skills imported, "
    f"{result.detached_source_runs} remote source runs kept as portable provenance."
)
PY

  printf '\n[%s] max_retrieved_skills=%s — recording Agent POV to %s\n\n' \
    "$condition" "$max_retrieved_skills" "$video_path"

  PYTHONPATH=backend/src backend/.venv/bin/python scripts/run_week10_live_training.py \
    --manifest-dir "$MANIFEST_PATH" \
    --task-id "$TASK_ID" \
    --host "$MINECRAFT_HOST" \
    --port "$MINECRAFT_PORT" \
    --worker-concurrency 1 \
    --max-steps-per-task 48 \
    --max-runtime-sec-per-task 900 \
    --database-path "$database_path" \
    --output "$live_report" \
    --rcon-reset \
    --rcon-host "${MINECRAFT_RCON_HOST:-127.0.0.1}" \
    --rcon-port "$MINECRAFT_RCON_PORT" \
    --clear-all-inventory-on-reset \
    --rcon-random-teleport-when-biome-missing \
    --threat-pause \
    --spectator-player "$MC_AGENT_SPECTATOR_PLAYER" \
    --record-agent-video \
    --recording-output "$video_path" \
    --recording-input "${MC_AGENT_RECORDING_INPUT:-Capture screen 0:none}" \
    "${visual_capture_args[@]}" \
    "$@" \
    --max-retrieved-skills "$max_retrieved_skills" \
    --min-skill-relevance "${MC_AGENT_MIN_SKILL_RELEVANCE:-0.5}"

  printf '[%s] complete. Audit database: %s\n' "$condition" "$database_url"
}

printf '\nRunning %s twice with independent random spawns and Agent POV recording.\n' "$TASK_ID"
printf 'The first run disables Skill retrieval; the second run leaves Skill retrieval enabled.\n'

case "$DEMO_MODE" in
  ab)
    run_condition "no_skill" 0 "$@"
    run_condition "with_skill" 3 "$@"
    ;;
  no-skill)
    run_condition "no_skill" 0 "$@"
    ;;
  with-skill)
    run_condition "with_skill" 3 "$@"
    ;;
esac

backend/.venv/bin/python - "$OUTPUT_DIR" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])


def summarize(condition: str) -> dict[str, object] | None:
    condition_dir = output_dir / condition
    report_path = condition_dir / "live_training.json"
    database_path = condition_dir / "audit.sqlite3"
    if not report_path.is_file():
        return None
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    outcomes = payload.get("outcomes") or []
    outcome = outcomes[0] if outcomes else {}
    usage = outcome.get("model_usage") or {}
    retrieved = set()
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT payload FROM trajectory_events WHERE event_type = 'context_built'"
        ).fetchall()
    for (raw_payload,) in rows:
        context = json.loads(raw_payload)
        for skill in context.get("retrieved_skills") or []:
            name = skill.get("name")
            if name:
                retrieved.add(str(name))
    return {
        "condition": condition,
        "success": outcome.get("success"),
        "steps": outcome.get("steps"),
        "duration_sec": outcome.get("duration_sec"),
        "model_calls": usage.get("model_call_count"),
        "total_tokens": usage.get("total_tokens"),
        "retrieved_skills": ", ".join(sorted(retrieved)) or "none",
        "report": str(report_path),
        "video": str(condition_dir / "agent_pov.mp4"),
    }


summaries = [
    summary
    for condition in ("no_skill", "with_skill")
    if (summary := summarize(condition)) is not None
]

print("\nProgrammatic demo complete")
for summary in summaries:
    print(f"\n  [{summary['condition']}]")
    print(f"    success:          {summary['success']}")
    print(f"    steps:            {summary['steps']}")
    print(f"    duration:         {summary['duration_sec']} sec")
    print(f"    model calls:      {summary['model_calls']}")
    print(f"    tokens:           {summary['total_tokens']}")
    print(f"    retrieved skills: {summary['retrieved_skills']}")
    print(f"    report:           {summary['report']}")
    print(f"    Agent POV:        {summary['video']}")

if len(summaries) == 2:
    left, right = summaries
    print("\n  [with_skill - no_skill]")
    for key in ("steps", "duration_sec", "model_calls", "total_tokens"):
        left_value = left.get(key)
        right_value = right.get(key)
        if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
            print(f"    {key}: {right_value - left_value:+.3f}")
PY
