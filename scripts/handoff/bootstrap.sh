#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WITH_INFRA=false
WITH_MINECRAFT=false
ACCEPT_EULA=false
CHECK_ONLY=false
SKIP_TESTS=false

usage() {
  printf '%s\n' \
    "Usage: $0 [--check-only] [--with-infra] [--with-minecraft]" \
    "          [--accept-minecraft-eula] [--skip-tests]" \
    "" \
    "--check-only               Check host prerequisites without modifying the project." \
    "--with-infra               Start PostgreSQL/pgvector and Redis with Docker Compose." \
    "--with-minecraft           Download and configure Minecraft 1.20.1 + Fabric + Carpet." \
    "--accept-minecraft-eula    Record explicit acceptance of the Minecraft server EULA." \
    "--skip-tests               Install dependencies without running the full CI suite."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check-only) CHECK_ONLY=true ;;
    --with-infra) WITH_INFRA=true ;;
    --with-minecraft) WITH_MINECRAFT=true ;;
    --accept-minecraft-eula) ACCEPT_EULA=true ;;
    --skip-tests) SKIP_TESTS=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

require_command() {
  local command_name="$1"
  local install_hint="$2"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    echo "Install hint: $install_hint" >&2
    exit 1
  fi
}

resolve_python() {
  local candidate
  local candidates=()
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    candidates+=("$PYTHON_BIN")
  fi
  candidates+=(python3.13 python3.12 python3.11 python3)
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! COMPATIBLE_PYTHON="$(resolve_python)"; then
  echo "Python 3.11+ is required." >&2
  echo "Install hint: Homebrew: brew install python@3.11; Ubuntu: apt install python3.11 python3.11-venv" >&2
  exit 1
fi
require_command node "Node.js 22 LTS (https://nodejs.org or nvm install 22)"
require_command npm "npm is bundled with Node.js"
require_command make "Xcode Command Line Tools on macOS, or build-essential on Ubuntu"
require_command curl "curl is required for health checks and optional runtime downloads"

"$COMPATIBLE_PYTHON" - <<'PY'
import sys

print(f"Python: {sys.version.split()[0]}")
PY

node - <<'NODE'
const [major, minor] = process.versions.node.split('.').map(Number);
const supported = (major === 20 && minor >= 19) || (major >= 22 && (major > 22 || minor >= 12));
if (!supported) {
  throw new Error(`Node.js 20.19+ or 22.12+ is required; found ${process.versions.node}`);
}
console.log(`Node.js: ${process.versions.node}`);
NODE

if "$WITH_MINECRAFT"; then
  require_command java "Java 17+ (Homebrew: brew install openjdk@17; Ubuntu: apt install openjdk-17-jre-headless)"
fi
if "$WITH_INFRA"; then
  require_command docker "Docker Desktop or Docker Engine with the Compose plugin"
  docker compose version >/dev/null
  docker info >/dev/null
fi
if "$CHECK_ONLY"; then
  echo "Host prerequisite check passed."
  exit 0
fi

cd "$ROOT_DIR"
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  "$COMPATIBLE_PYTHON" - .env <<'PY'
from pathlib import Path
import secrets
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
rcon_password = secrets.token_hex(24)
postgres_password = secrets.token_hex(24)
text = text.replace(
    "MINECRAFT_RCON_PASSWORD=replace-me",
    f"MINECRAFT_RCON_PASSWORD={rcon_password}",
)
text = text.replace(
    "DATABASE_URL=postgresql+psycopg://mc_agent:mc_agent@localhost:5432/mc_agent",
    "DATABASE_URL=postgresql+psycopg://"
    f"mc_agent:{postgres_password}@localhost:5432/mc_agent",
)
text = text.replace(
    "POSTGRES_PASSWORD=mc_agent",
    f"POSTGRES_PASSWORD={postgres_password}",
)
path.write_text(text, encoding="utf-8")
PY
  echo "Created .env with generated RCON and PostgreSQL passwords. QWEN_API_KEY remains unset."
else
  echo "Keeping existing .env."
fi

"$COMPATIBLE_PYTHON" -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install \
  -c backend/constraints-handoff.txt \
  -e "backend[dev]"
npm ci --prefix workers/mineflayer-worker
npm ci --prefix frontend

MANIFEST_PATH="tasks/executable/minedojo_programmatic_tasks.jsonl"
if [[ ! -s "$MANIFEST_PATH" ]]; then
  build_args=(
    scripts/build_minedojo_executable_manifests.py
    --output-jsonl "$MANIFEST_PATH"
    --summary-path tasks/executable/minedojo_programmatic_tasks.summary.json
    --pretty
  )
  if [[ -f tasks/sources/minedojo/tasks_specs.yaml ]]; then
    build_args+=(--tasks-specs-file tasks/sources/minedojo/tasks_specs.yaml)
  fi
  PYTHONPATH=backend/src backend/.venv/bin/python "${build_args[@]}"
fi

if ! "$SKIP_TESTS"; then
  make ci
fi

if "$WITH_INFRA"; then
  docker compose up -d postgres redis
  for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U mc_agent -d mc_agent >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  docker compose exec -T postgres pg_isready -U mc_agent -d mc_agent >/dev/null
  PYTHONPATH=backend/src backend/.venv/bin/python -m alembic upgrade head
  PYTHONPATH=backend/src backend/.venv/bin/python scripts/seed_knowledge_chunks.py
fi

if "$WITH_MINECRAFT"; then
  minecraft_args=()
  if "$ACCEPT_EULA"; then
    minecraft_args+=(--accept-eula)
  fi
  scripts/handoff/setup_minecraft_server.sh "${minecraft_args[@]}"
fi

cat <<'EOF'

Bootstrap completed.

Next steps:
1. Set QWEN_API_KEY in .env or export it in the shell.
2. Run an offline demo: ./scripts/handoff/run_demo.sh offline
3. Run a live demo:    ./scripts/handoff/run_demo.sh live
4. Start the dashboard after a run using the command printed by run_demo.sh.
EOF
