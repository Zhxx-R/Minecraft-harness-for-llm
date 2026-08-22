#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_DIR="$ROOT_DIR/infra/minecraft-server"
INSTALLER_DIR="$SERVER_DIR/installers"
MODS_DIR="$SERVER_DIR/mods"
ACCEPT_EULA=false
VANILLA_ONLY=false

MOJANG_SERVER_URL="https://piston-data.mojang.com/v1/objects/84194a2f286ef7c14ed7ce0090dba59902951553/server.jar"
MOJANG_SERVER_SHA1="84194a2f286ef7c14ed7ce0090dba59902951553"
FABRIC_INSTALLER_URL="https://maven.fabricmc.net/net/fabricmc/fabric-installer/1.1.1/fabric-installer-1.1.1.jar"
FABRIC_INSTALLER_SHA256="2487a69dd6f9d9c2605265a7142d77c26ab62edc620e6bcf810d581d2ee31b79"
FABRIC_LOADER_VERSION="0.19.3"
CARPET_URL="https://github.com/gnembon/fabric-carpet/releases/download/1.4.112/fabric-carpet-1.20-1.4.112%2Bv230608.jar"
CARPET_SHA256="00ad0ed15c457fdec0e6eefe84d79e1bb7b8f91f5f3a133cf89cb2b60ffb3d11"

usage() {
  printf '%s\n' \
    "Usage: $0 --accept-eula [--vanilla]" \
    "" \
    "The script only binds the generated server to 127.0.0.1." \
    "--accept-eula records that the user has read and accepted Minecraft's EULA." \
    "--vanilla skips Fabric and Carpet; threat-pause will then be unavailable."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --accept-eula) ACCEPT_EULA=true ;;
    --vanilla) VANILLA_ONLY=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "${MINECRAFT_EULA:-}" == "TRUE" || "${MINECRAFT_EULA:-}" == "true" ]]; then
  ACCEPT_EULA=true
fi
if ! "$ACCEPT_EULA"; then
  echo "Minecraft EULA acceptance is required." >&2
  echo "Read https://aka.ms/MinecraftEULA, then rerun with --accept-eula." >&2
  exit 1
fi

for command_name in curl java python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Missing required command: $command_name" >&2
    exit 1
  fi
done

EXTERNAL_RCON_PASSWORD="${MINECRAFT_RCON_PASSWORD:-}"
EXTERNAL_SERVER_PORT="${MINECRAFT_PORT:-}"
EXTERNAL_RCON_PORT="${MINECRAFT_RCON_PORT:-}"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi
[[ -n "$EXTERNAL_RCON_PASSWORD" ]] && MINECRAFT_RCON_PASSWORD="$EXTERNAL_RCON_PASSWORD"
[[ -n "$EXTERNAL_SERVER_PORT" ]] && MINECRAFT_PORT="$EXTERNAL_SERVER_PORT"
[[ -n "$EXTERNAL_RCON_PORT" ]] && MINECRAFT_RCON_PORT="$EXTERNAL_RCON_PORT"

RCON_PASSWORD="${MINECRAFT_RCON_PASSWORD:-}"
SERVER_PORT="${MINECRAFT_PORT:-25565}"
RCON_PORT="${MINECRAFT_RCON_PORT:-25575}"
if [[ ! "$RCON_PASSWORD" =~ ^[A-Za-z0-9._-]{8,128}$ || "$RCON_PASSWORD" == "replace-me" ]]; then
  echo "Set a strong MINECRAFT_RCON_PASSWORD in .env before server setup." >&2
  exit 1
fi
if [[ ! "$SERVER_PORT" =~ ^[0-9]+$ || ! "$RCON_PORT" =~ ^[0-9]+$ ]]; then
  echo "MINECRAFT_PORT and MINECRAFT_RCON_PORT must be numeric." >&2
  exit 1
fi

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

sha1_file() {
  if command -v sha1sum >/dev/null 2>&1; then
    sha1sum "$1" | awk '{print $1}'
  else
    shasum -a 1 "$1" | awk '{print $1}'
  fi
}

download_verified() {
  local url="$1"
  local destination="$2"
  local algorithm="$3"
  local expected="$4"
  local actual=""
  if [[ -f "$destination" ]]; then
    actual="$(if [[ "$algorithm" == "sha1" ]]; then sha1_file "$destination"; else sha256_file "$destination"; fi)"
  fi
  if [[ "$actual" != "$expected" ]]; then
    mkdir -p "$(dirname "$destination")"
    curl --fail --location --retry 3 --output "$destination.tmp" "$url"
    actual="$(if [[ "$algorithm" == "sha1" ]]; then sha1_file "$destination.tmp"; else sha256_file "$destination.tmp"; fi)"
    if [[ "$actual" != "$expected" ]]; then
      rm -f "$destination.tmp"
      echo "Checksum mismatch for $url" >&2
      echo "Expected $expected but received $actual" >&2
      exit 1
    fi
    mv "$destination.tmp" "$destination"
  fi
}

mkdir -p "$SERVER_DIR" "$INSTALLER_DIR" "$MODS_DIR"
download_verified "$MOJANG_SERVER_URL" "$SERVER_DIR/server.jar" sha1 "$MOJANG_SERVER_SHA1"
cp "$SERVER_DIR/server.jar" "$SERVER_DIR/server-1.20.1.jar"

if ! "$VANILLA_ONLY"; then
  FABRIC_INSTALLER="$INSTALLER_DIR/fabric-installer-1.1.1.jar"
  download_verified "$FABRIC_INSTALLER_URL" "$FABRIC_INSTALLER" sha256 "$FABRIC_INSTALLER_SHA256"
  java -jar "$FABRIC_INSTALLER" server \
    -dir "$SERVER_DIR" \
    -mcversion 1.20.1 \
    -loader "$FABRIC_LOADER_VERSION"
  download_verified \
    "$CARPET_URL" \
    "$MODS_DIR/fabric-carpet-1.20-1.4.112+v230608.jar" \
    sha256 \
    "$CARPET_SHA256"
fi

python3 - \
  "$ROOT_DIR/configs/minecraft/server.properties.template" \
  "$SERVER_DIR/server.properties" \
  "$SERVER_PORT" \
  "$RCON_PORT" \
  "$RCON_PASSWORD" <<'PY'
from pathlib import Path
import sys

template_path, output_path, server_port, rcon_port, rcon_password = sys.argv[1:]
text = Path(template_path).read_text(encoding="utf-8")
text = text.replace("__SERVER_PORT__", server_port)
text = text.replace("__RCON_PORT__", rcon_port)
text = text.replace("__RCON_PASSWORD__", rcon_password)
Path(output_path).write_text(text, encoding="utf-8")
PY
printf 'eula=true\n' > "$SERVER_DIR/eula.txt"
chmod 600 "$SERVER_DIR/server.properties"

echo "Minecraft 1.20.1 server prepared in $SERVER_DIR"
if "$VANILLA_ONLY"; then
  echo "Runtime: vanilla (threat-pause unavailable)"
else
  echo "Runtime: Fabric $FABRIC_LOADER_VERSION + Carpet 1.4.112"
fi
echo "Start with: scripts/start_minecraft_server.sh --background"
