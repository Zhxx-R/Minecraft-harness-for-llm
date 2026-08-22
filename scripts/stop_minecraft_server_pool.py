from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    """Parse CLI options for stopping a generated Minecraft server pool."""

    parser = argparse.ArgumentParser(description="Stop a local multi-port Minecraft server pool.")
    parser.add_argument("--pool-dir", type=Path, default=ROOT / "infra" / "minecraft-server-pool")
    parser.add_argument("--timeout-sec", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    """Terminate every server process recorded under the pool directory."""

    args = parse_args()
    pid_files = sorted(args.pool_dir.glob("server-*/server.pid"))
    results = [_stop_pid_file(path, timeout_sec=args.timeout_sec) for path in pid_files]
    state_path = args.pool_dir / "server_pool_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["stop_results"] = results
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"pool_dir": str(args.pool_dir), "stopped": results}, indent=2, sort_keys=True))


def _stop_pid_file(path: Path, *, timeout_sec: float) -> dict[str, object]:
    """Terminate the process referenced by one server.pid file."""

    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return {"pid_file": str(path), "ok": False, "reason": "invalid_pid"}
    if not _is_running(pid):
        path.unlink(missing_ok=True)
        return {"pid_file": str(path), "pid": pid, "ok": True, "reason": "already_stopped"}
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not _is_running(pid):
            path.unlink(missing_ok=True)
            return {"pid_file": str(path), "pid": pid, "ok": True, "reason": "terminated"}
        time.sleep(0.25)
    os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return {"pid_file": str(path), "pid": pid, "ok": True, "reason": "killed_after_timeout"}


def _is_running(pid: int) -> bool:
    """Return whether a process id appears to be alive."""

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    main()
