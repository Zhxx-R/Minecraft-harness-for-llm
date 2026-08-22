from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.runtime.server_pool import (  # noqa: E402
    build_local_server_pool,
    estimate_server_pool_resources,
)


def parse_args() -> argparse.Namespace:
    """Parse CLI options for starting isolated local Minecraft server instances."""

    parser = argparse.ArgumentParser(description="Start a local multi-port Minecraft server pool.")
    parser.add_argument("--base-dir", type=Path, default=ROOT / "infra" / "minecraft-server")
    parser.add_argument("--pool-dir", type=Path, default=ROOT / "infra" / "minecraft-server-pool")
    parser.add_argument("--server-count", type=int, default=2)
    parser.add_argument("--first-server-port", type=int, default=25565)
    parser.add_argument("--first-rcon-port", type=int, default=25575)
    parser.add_argument("--rcon-password", default=os.getenv("MINECRAFT_RCON_PASSWORD"))
    parser.add_argument("--heap-gb", type=float, default=2.5)
    parser.add_argument("--java-bin", default=os.getenv("JAVA", "java"))
    parser.add_argument("--startup-timeout-sec", type=float, default=180.0)
    parser.add_argument("--force", action="store_true", help="Overwrite generated server.properties files.")
    return parser.parse_args()


def main() -> None:
    """Create server pool directories, launch Java servers, and write a pool state file."""

    args = parse_args()
    if not args.rcon_password:
        raise SystemExit("Set MINECRAFT_RCON_PASSWORD or pass --rcon-password.")
    vanilla_jar = args.base_dir / "server-1.20.1.jar"
    fabric_jar = args.base_dir / "fabric-server-launch.jar"
    server_launcher = fabric_jar if fabric_jar.exists() else vanilla_jar
    if not server_launcher.exists():
        raise SystemExit(f"Missing Minecraft server launcher: {server_launcher}")

    pool = build_local_server_pool(
        root_dir=args.pool_dir,
        server_count=args.server_count,
        first_server_port=args.first_server_port,
        first_rcon_port=args.first_rcon_port,
        heap_gb=args.heap_gb,
        host="127.0.0.1",
    )
    estimate = estimate_server_pool_resources(pool)
    args.pool_dir.mkdir(parents=True, exist_ok=True)
    processes = []
    started_processes: list[subprocess.Popen[bytes]] = []
    for server in pool:
        server_dir = args.pool_dir / server.server_id
        _prepare_server_dir(
            server_dir=server_dir,
            base_dir=args.base_dir,
            server_launcher=server_launcher,
            server_port=server.server_port,
            rcon_port=server.rcon_port or 25575,
            rcon_password=args.rcon_password,
            force=args.force,
        )
        pid_file = server_dir / "server.pid"
        if pid_file.exists() and _is_running(pid_file):
            processes.append({"server_id": server.server_id, "pid": int(pid_file.read_text().strip()), "already_running": True})
            continue
        log_file = server_dir / "server.log"
        log_handle = log_file.open("ab", buffering=0)
        process = subprocess.Popen(
            [
                args.java_bin,
                f"-Xms{_heap_megabytes(server.heap_gb)}M",
                f"-Xmx{_heap_megabytes(server.heap_gb)}M",
                "-jar",
                str(server_dir / server_launcher.name),
                "nogui",
            ],
            cwd=server_dir,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log_handle.close()
        started_processes.append(process)
        pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
        processes.append({"server_id": server.server_id, "pid": process.pid, "already_running": False})

    readiness: list[dict[str, object]] = []
    try:
        for server in pool:
            started_at = time.monotonic()
            _wait_for_tcp(server.host, server.server_port, args.startup_timeout_sec)
            if server.rcon_port is not None:
                _wait_for_tcp(server.host, server.rcon_port, args.startup_timeout_sec)
            readiness.append(
                {
                    "server_id": server.server_id,
                    "game_port_ready": True,
                    "rcon_port_ready": server.rcon_port is not None,
                    "startup_wait_sec": round(time.monotonic() - started_at, 3),
                }
            )
    except TimeoutError:
        for process in started_processes:
            if process.poll() is None:
                process.terminate()
        raise

    state = {
        "started_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "pool_dir": str(args.pool_dir),
        "servers": [server.to_json() for server in pool],
        "processes": processes,
        "readiness": readiness,
        "resource_estimate": estimate.to_json(),
    }
    state_path = args.pool_dir / "server_pool_state.json"
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({**state, "rcon_password": "<redacted>", "state_path": str(state_path)}, indent=2, sort_keys=True))


def _prepare_server_dir(
    *,
    server_dir: Path,
    base_dir: Path,
    server_launcher: Path,
    server_port: int,
    rcon_port: int,
    rcon_password: str,
    force: bool,
) -> None:
    """Create one isolated server directory with patched server.properties."""

    server_dir.mkdir(parents=True, exist_ok=True)
    _link_or_copy(server_launcher, server_dir / server_launcher.name)
    if server_launcher.name == "fabric-server-launch.jar":
        _link_or_copy(base_dir / "server.jar", server_dir / "server.jar")
        _link_or_copy(base_dir / "libraries", server_dir / "libraries")
        _link_or_copy(base_dir / "versions", server_dir / "versions")
        _copy_mods(base_dir / "mods", server_dir / "mods")
    eula = server_dir / "eula.txt"
    if not eula.exists():
        eula.write_text("eula=true\n", encoding="utf-8")
    properties = server_dir / "server.properties"
    if properties.exists() and not force:
        return
    template_path = base_dir / "server.properties"
    template = _read_properties(template_path) if template_path.exists() else {}
    template.update(
        {
            "server-port": str(server_port),
            "enable-rcon": "true",
            "rcon.port": str(rcon_port),
            "rcon.password": rcon_password,
            "online-mode": template.get("online-mode", "false"),
            "allow-flight": "true",
            "spawn-protection": "0",
        }
    )
    properties.write_text(_render_properties(template), encoding="utf-8")


def _link_or_copy(source: Path, target: Path) -> None:
    """Link a shared server dependency into an isolated server directory."""

    if target.exists() or target.is_symlink():
        return
    if source.is_dir():
        try:
            target.symlink_to(source, target_is_directory=True)
        except OSError:
            shutil.copytree(source, target)
        return
    try:
        target.symlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def _copy_mods(source: Path, target: Path) -> None:
    """Copy Fabric server mods so each pool instance can be audited independently."""

    if not source.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for mod_file in source.glob("*.jar"):
        destination = target / mod_file.name
        if not destination.exists():
            shutil.copy2(mod_file, destination)


def _read_properties(path: Path) -> dict[str, str]:
    """Read a Java .properties file into a simple string dictionary."""

    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def _render_properties(values: dict[str, str]) -> str:
    """Render deterministic server.properties content."""

    return "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"


def _is_running(pid_file: Path) -> bool:
    """Return whether the pid in a pid file appears to be alive."""

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (ValueError, OSError):
        return False
    return True


def _heap_megabytes(heap_gb: float) -> int:
    """Convert a decimal GiB configuration into a valid JVM megabyte argument."""

    if heap_gb <= 0:
        raise ValueError("heap-gb must be positive.")
    return max(1024, int(round(heap_gb * 1024)))


def _wait_for_tcp(host: str, port: int, timeout_sec: float) -> None:
    """Wait until one server endpoint accepts TCP connections."""

    deadline = time.monotonic() + timeout_sec
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for {host}:{port}: {last_error}")


if __name__ == "__main__":
    main()
