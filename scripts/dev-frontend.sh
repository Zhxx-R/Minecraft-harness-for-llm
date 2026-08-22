#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../frontend"
: "${VITE_API_BASE_URL:=http://127.0.0.1:8000}"
export VITE_API_BASE_URL
npm run dev
