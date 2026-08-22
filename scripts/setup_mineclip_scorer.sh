#!/usr/bin/env bash
set -euo pipefail

# MineCLIP is kept in a dedicated environment because its research dependencies are not backend dependencies.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="${ROOT_DIR}/services/mineclip-scorer"
VENV_DIR="${SERVICE_DIR}/.venv"
VENDOR_DIR="${SERVICE_DIR}/vendor/MineCLIP"
CHECKPOINT_DIR="${SERVICE_DIR}/checkpoints"
CACHE_DIR="${SERVICE_DIR}/cache/huggingface"
LOCAL_ENV="${SERVICE_DIR}/.env.local"
MINECLIP_REVISION="e6c06a0245fac63dceb38bc9bd4fecd033dae735"
VARIANT="attn"
DOWNLOAD_CHECKPOINT=1
PREFETCH_TOKENIZER=1

usage() {
  printf '%s\n' \
    'Usage: scripts/setup_mineclip_scorer.sh [options]' \
    '' \
    'Options:' \
    '  --variant attn|avg        Official MineCLIP checkpoint variant (default: attn).' \
    '  --skip-checkpoint         Install code only; do not download the official checkpoint.' \
    '  --skip-tokenizer-prefetch Do not prefetch the small CLIP tokenizer cache.' \
    '  -h, --help                Show this help.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)
      VARIANT="${2:-}"
      shift 2
      ;;
    --skip-checkpoint)
      DOWNLOAD_CHECKPOINT=0
      shift
      ;;
    --skip-tokenizer-prefetch)
      PREFETCH_TOKENIZER=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$VARIANT" in
  attn)
    CHECKPOINT_FILE="attn.pth"
    CHECKPOINT_FILE_ID="1uaZM1ZLBz2dZWcn85rZmjP7LV6Sg5PZW"
    CHECKPOINT_MD5="b5ece9198337cfd117a3bfbd921e56da"
    ;;
  avg)
    CHECKPOINT_FILE="avg.pth"
    CHECKPOINT_FILE_ID="1mFe09JsVS5FpZ82yuV7fYNFYnkz9jDqr"
    CHECKPOINT_MD5="d97a07f2830095a2016a8da22abcff52"
    ;;
  *)
    printf 'Unsupported MineCLIP variant: %s\n' "$VARIANT" >&2
    exit 2
    ;;
esac

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "${ROOT_DIR}/backend/.venv/bin/python" ]]; then
    PYTHON_BIN="${ROOT_DIR}/backend/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi
fi
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  printf 'MineCLIP setup requires Python 3.11 or newer. Set PYTHON_BIN explicitly.\n' >&2
  exit 1
fi

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/python" -m pip install --upgrade pip wheel
"${VENV_DIR}/bin/python" -m pip install -r "${SERVICE_DIR}/requirements.txt"
"${VENV_DIR}/bin/python" -m pip install -r "${SERVICE_DIR}/requirements-setup.txt"

if [[ ! -d "${VENDOR_DIR}/.git" ]]; then
  mkdir -p "$(dirname "${VENDOR_DIR}")"
  git clone https://github.com/MineDojo/MineCLIP.git "${VENDOR_DIR}"
fi
git -C "${VENDOR_DIR}" fetch --depth 1 origin "${MINECLIP_REVISION}"
git -C "${VENDOR_DIR}" checkout --detach "${MINECLIP_REVISION}"

CHECKPOINT_PATH="${CHECKPOINT_DIR}/${CHECKPOINT_FILE}"
mkdir -p "${CHECKPOINT_DIR}" "${CACHE_DIR}"
if [[ "$DOWNLOAD_CHECKPOINT" -eq 1 ]]; then
  CURRENT_MD5=""
  if [[ -f "$CHECKPOINT_PATH" ]]; then
    CURRENT_MD5="$("${VENV_DIR}/bin/python" - "$CHECKPOINT_PATH" <<'PY'
import hashlib
import sys

digest = hashlib.md5()  # The upstream release contract publishes MD5 checksums.
with open(sys.argv[1], "rb") as stream:
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
  fi
  if [[ "$CURRENT_MD5" != "$CHECKPOINT_MD5" ]]; then
    rm -f "${CHECKPOINT_PATH}.partial"
    "${VENV_DIR}/bin/python" -m gdown \
      "https://drive.google.com/uc?id=${CHECKPOINT_FILE_ID}" \
      -O "${CHECKPOINT_PATH}.partial"
    mv "${CHECKPOINT_PATH}.partial" "$CHECKPOINT_PATH"
  fi
  ACTUAL_MD5="$("${VENV_DIR}/bin/python" - "$CHECKPOINT_PATH" <<'PY'
import hashlib
import sys

digest = hashlib.md5()  # The upstream release contract publishes MD5 checksums.
with open(sys.argv[1], "rb") as stream:
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
  if [[ "$ACTUAL_MD5" != "$CHECKPOINT_MD5" ]]; then
    printf 'Checkpoint checksum mismatch: %s != %s\n' "$ACTUAL_MD5" "$CHECKPOINT_MD5" >&2
    exit 1
  fi
fi

if [[ "$PREFETCH_TOKENIZER" -eq 1 ]]; then
  HF_HOME="$CACHE_DIR" "${VENV_DIR}/bin/python" - <<'PY'
from transformers import AutoTokenizer

AutoTokenizer.from_pretrained("openai/clip-vit-base-patch16", use_fast=True)
print("CLIP tokenizer cache is ready.")
PY
fi

{
  printf 'export MINECLIP_VARIANT=%q\n' "$VARIANT"
  printf 'export MINECLIP_CHECKPOINT=%q\n' "$CHECKPOINT_PATH"
  printf 'export MINECLIP_REPOSITORY=%q\n' "$VENDOR_DIR"
  printf 'export MINECLIP_DEVICE=%q\n' "auto"
  printf 'export HF_HOME=%q\n' "$CACHE_DIR"
} > "$LOCAL_ENV"

printf 'MineCLIP scorer environment ready at %s\n' "$SERVICE_DIR"
printf 'Variant: %s\n' "$VARIANT"
if [[ -f "$CHECKPOINT_PATH" ]]; then
  printf 'Checkpoint: %s (%s)\n' "$CHECKPOINT_PATH" "$CHECKPOINT_MD5"
else
  printf 'Checkpoint download skipped; configure MINECLIP_CHECKPOINT before startup.\n'
fi
printf 'Local runtime configuration: %s\n' "$LOCAL_ENV"
