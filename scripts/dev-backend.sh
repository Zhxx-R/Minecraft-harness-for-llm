#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../backend"
.venv/bin/uvicorn mc_agent_harness.main:app --reload
