#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# With five workers, six attempts per task, and a 600-second attempt cap,
# 100 tasks consume at most 20 hours of bounded task execution. The remaining
# four hours are reserved for reset, provider throttling, and process overhead.
export WEEK10_WORKER_COUNT="${WEEK10_WORKER_COUNT:-5}"
export WEEK10_TASK_COUNT="${WEEK10_TASK_COUNT:-100}"
export WEEK10_MAX_TASK_RETRIES="${WEEK10_MAX_TASK_RETRIES:-5}"
export WEEK10_MAX_TASK_SIMILARITY="${WEEK10_MAX_TASK_SIMILARITY:-1.0}"
export WEEK10_RUN_LABEL="${WEEK10_RUN_LABEL:-week10-24h}"

exec "$ROOT_DIR/scripts/handoff/run_week10_full_5.sh" "$@"
