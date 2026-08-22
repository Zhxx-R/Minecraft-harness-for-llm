#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT_DIR/infra/minecraft-server"
FABRIC_JAR="$SERVER_DIR/fabric-server-launch.jar"
VANILLA_JAR="$SERVER_DIR/server-1.20.1.jar"
if [[ -f "$FABRIC_JAR" ]]; then
  SERVER_JAR="$FABRIC_JAR"
else
  SERVER_JAR="$VANILLA_JAR"
fi
PID_FILE="$SERVER_DIR/server.pid"
LOG_FILE="$SERVER_DIR/server.log"
JAVA_BIN="${JAVA_BIN:-java}"
JAVA_XMS="${MC_SERVER_XMS:-1G}"
JAVA_XMX="${MC_SERVER_XMX:-3G}"

if [[ ! -f "$SERVER_JAR" ]]; then
  echo "Missing $SERVER_JAR" >&2
  echo "Run the server setup step or download the Minecraft 1.20.1 server jar first." >&2
  exit 1
fi

cd "$SERVER_DIR"
if [[ "${1:-}" == "--background" ]]; then
  if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Minecraft server is already running with PID $(cat "$PID_FILE")."
    exit 0
  fi
  python3 - "$SERVER_DIR" "$SERVER_JAR" "$LOG_FILE" "$PID_FILE" "$JAVA_BIN" "$JAVA_XMS" "$JAVA_XMX" <<'PY'
import os
import subprocess
import sys

server_dir, server_jar, log_file, pid_file, java_bin, java_xms, java_xmx = sys.argv[1:]
log_handle = open(log_file, "ab", buffering=0)
process = subprocess.Popen(
    [java_bin, f"-Xms{java_xms}", f"-Xmx{java_xmx}", "-jar", server_jar, "nogui"],
    cwd=server_dir,
    stdin=subprocess.DEVNULL,
    stdout=log_handle,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    close_fds=True,
)
with open(pid_file, "w", encoding="utf-8") as pid_handle:
    pid_handle.write(f"{process.pid}\n")
print(f"Minecraft server started with PID {process.pid}. Log: {log_file}")
PY
else
  exec "$JAVA_BIN" "-Xms$JAVA_XMS" "-Xmx$JAVA_XMX" -jar "$SERVER_JAR" nogui
fi
