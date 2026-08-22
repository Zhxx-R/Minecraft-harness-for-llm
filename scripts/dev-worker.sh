#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../workers/mineflayer-worker"
npm run dev

