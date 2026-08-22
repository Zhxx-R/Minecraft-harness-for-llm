from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """Parse one detached process launch request."""

    parser = argparse.ArgumentParser(description="Launch one handoff service in a new session.")
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> None:
    """Start the requested process and persist its operating-system PID."""

    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("A command is required after --.")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("ab", buffering=0) as log_handle:
        process = subprocess.Popen(
            command,
            cwd=args.cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    args.pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
    print(process.pid)


if __name__ == "__main__":
    main()
