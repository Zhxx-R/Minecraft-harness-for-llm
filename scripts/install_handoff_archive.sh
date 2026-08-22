#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 ARCHIVE.tar.gz TARGET_DIR [bootstrap options...]" >&2
  echo "Example: $0 minecraft-agent-harness-handoff.tar.gz ~/minecraft-agent --skip-tests" >&2
  exit 2
fi

ARCHIVE="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
TARGET_DIR="$2"
shift 2

if [[ ! -f "$ARCHIVE" ]]; then
  echo "Archive not found: $ARCHIVE" >&2
  exit 1
fi
if [[ -e "$TARGET_DIR" ]] && [[ -n "$(find "$TARGET_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Target directory is not empty: $TARGET_DIR" >&2
  exit 1
fi
mkdir -p "$TARGET_DIR"

CHECKSUM_FILE="$ARCHIVE.sha256"
if [[ -f "$CHECKSUM_FILE" ]]; then
  expected="$(awk '{print $1}' "$CHECKSUM_FILE")"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
  else
    actual="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "Archive checksum mismatch." >&2
    exit 1
  fi
fi

tar -xzf "$ARCHIVE" --strip-components=1 -C "$TARGET_DIR"
cd "$TARGET_DIR"
./scripts/handoff/bootstrap.sh "$@"
