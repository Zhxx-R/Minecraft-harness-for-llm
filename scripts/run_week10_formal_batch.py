from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tasks" / "executable" / "minedojo_programmatic_tasks.jsonl"
DEFAULT_POOL_STATE = (
    ROOT / "infra" / "minecraft-server-pool" / "server_pool_state.json"
)


def parse_args() -> argparse.Namespace:
    """Parse the conservative two-server Week 10 formal-batch options."""

    parser = argparse.ArgumentParser(
        description=(
            "Run an auditable MineDojo programmatic batch with isolated Minecraft servers, "
            "bounded model concurrency, retries, and batch-barrier skill updates."
        )
    )
    parser.add_argument("--task-count", type=int, default=100)
    parser.add_argument("--worker-concurrency", type=int, default=2)
    parser.add_argument(
        "--max-task-similarity",
        type=float,
        default=0.45,
        help=(
            "Hard pairwise similarity ceiling inside each concurrent wave. Use 1.0 "
            "for best-effort diversity while keeping all workers filled."
        ),
    )
    parser.add_argument(
        "--include-survival",
        action="store_true",
        help=(
            "Include the two survival tasks in automatic selection. Without this flag, "
            "the established formal scope is harvest/combat/techtree only."
        ),
    )
    parser.add_argument("--max-task-retries", type=int, default=5)
    parser.add_argument("--max-steps-per-task", type=int, default=30)
    parser.add_argument("--max-runtime-sec-per-task", type=float, default=600.0)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--server-pool-state", type=Path, default=DEFAULT_POOL_STATE)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--start-docker", action="store_true")
    parser.add_argument("--start-server-pool", action="store_true")
    parser.add_argument("--server-heap-gb", type=float, default=2.5)
    parser.add_argument("--first-server-port", type=int, default=25565)
    parser.add_argument("--first-rcon-port", type=int, default=25575)
    parser.add_argument("--no-threat-pause", action="store_true")
    parser.add_argument("--no-auto-promote", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume completed waves from the checkpoint under --output-dir.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Optionally start dependencies and execute one formal Week 10 batch."""

    args = parse_args()
    _validate_args(args)
    password = os.getenv("MINECRAFT_RCON_PASSWORD")
    if not password and not args.dry_run:
        raise SystemExit("Set MINECRAFT_RCON_PASSWORD before formal live training.")
    if not args.manifest.is_file():
        raise SystemExit(f"Executable MineDojo manifest was not found: {args.manifest}")

    if args.start_docker and not args.dry_run:
        _run_streaming(
            ["docker", "compose", "up", "-d", "postgres", "redis"],
            log_path=None,
        )
    if args.start_server_pool and not args.dry_run:
        _run_streaming(_server_pool_command(args), log_path=None)
    if not args.server_pool_state.is_file() and not args.dry_run:
        raise SystemExit(
            f"Server pool state was not found: {args.server_pool_state}. "
            "Pass --start-server-pool or start the pool separately."
        )

    output_dir = args.output_dir or ROOT / "runs" / "formal" / _timestamp()
    output_path = output_dir / "week10_formal_batch.json"
    log_path = output_dir / "week10_formal_batch.log"
    command = _live_training_command(args, output_path)
    print(_shell_command(command))
    print(f"Audit report: {output_path}")
    print(f"Terminal log: {log_path}")
    if args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        _run_streaming(command, log_path=log_path)
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    _run_streaming(command, log_path=log_path)


def _validate_args(args: argparse.Namespace) -> None:
    """Reject formal-batch settings that cannot express useful work."""

    if args.task_count <= 0:
        raise SystemExit("--task-count must be positive.")
    if args.worker_concurrency <= 0:
        raise SystemExit("--worker-concurrency must be positive.")
    if not 0.0 <= args.max_task_similarity <= 1.0:
        raise SystemExit("--max-task-similarity must be between 0.0 and 1.0.")
    if args.max_task_retries < 0:
        raise SystemExit("--max-task-retries must be non-negative.")
    if args.server_heap_gb < 1:
        raise SystemExit("--server-heap-gb must be at least 1.")
    if args.resume and args.output_dir is None:
        raise SystemExit("--resume requires the original --output-dir.")
    if args.resume and args.dry_run:
        raise SystemExit("--resume cannot be combined with --dry-run.")


def _server_pool_command(args: argparse.Namespace) -> list[str]:
    """Build the isolated Minecraft server-pool startup command."""

    return [
        _python_bin(),
        "scripts/start_minecraft_server_pool.py",
        "--server-count",
        str(args.worker_concurrency),
        "--first-server-port",
        str(args.first_server_port),
        "--first-rcon-port",
        str(args.first_rcon_port),
        "--heap-gb",
        str(args.server_heap_gb),
    ]


def _live_training_command(args: argparse.Namespace, output_path: Path) -> list[str]:
    """Build the full two-server programmatic training command."""

    command = [
        _python_bin(),
        "scripts/run_week10_live_training.py",
        "--manifest-dir",
        str(args.manifest),
        "--diverse-batch-size",
        str(args.task_count),
        "--stratified-batch",
        "--max-task-similarity",
        str(getattr(args, "max_task_similarity", 0.45)),
        "--worker-concurrency",
        str(args.worker_concurrency),
        "--server-pool-state",
        str(args.server_pool_state),
        "--max-task-retries",
        str(args.max_task_retries),
        "--max-steps-per-task",
        str(args.max_steps_per_task),
        "--max-runtime-sec-per-task",
        str(args.max_runtime_sec_per_task),
        "--model-concurrency",
        str(args.worker_concurrency),
        "--provider-transient-retries",
        "2",
        "--model-timeout-retries",
        "2",
        "--model-timeout-requeues",
        "1",
        "--rcon-reset",
        "--rcon-random-teleport-when-biome-missing",
        "--clear-all-inventory-on-reset",
        "--output",
        str(output_path),
        "--checkpoint-path",
        str(output_path.with_name("week10_formal_batch.checkpoint.json")),
    ]
    categories = ["harvest", "combat", "techtree"]
    if getattr(args, "include_survival", False):
        categories.append("survival")
    for category in categories:
        command.extend(["--category", category])
    if args.database_url:
        command.extend(["--database-url", args.database_url])
    if not args.no_threat_pause:
        command.append("--threat-pause")
    if not args.no_auto_promote:
        command.append("--auto-promote")
    if getattr(args, "dry_run", False):
        command.append("--plan-only")
    if getattr(args, "resume", False):
        command.append("--resume")
    return command


def _run_streaming(command: list[str], *, log_path: Path | None) -> None:
    """Run one child process while streaming and optionally preserving terminal output."""

    print(f"\n$ {_shell_command(command)}\n")
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") if log_path is not None else _NullLog() as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        return_code = process.wait()
    if return_code != 0:
        raise SystemExit(return_code)


class _NullLog:
    """No-op context manager matching the small file interface used by the runner."""

    def __enter__(self) -> _NullLog:
        """Return the no-op log sink."""

        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Finish the no-op context without suppressing errors."""

        return None

    def write(self, _text: str) -> int:
        """Accept one log line without storing it."""

        return 0

    def flush(self) -> None:
        """Provide the flush method expected by the streaming loop."""

        return None


def _child_env() -> dict[str, str]:
    """Build a child environment with the backend package import path."""

    env = os.environ.copy()
    backend_src = str(ROOT / "backend" / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{backend_src}{os.pathsep}{existing}" if existing else backend_src
    return env


def _python_bin() -> str:
    """Return the repository virtual-environment Python executable."""

    return str(ROOT / "backend" / ".venv" / "bin" / "python")


def _shell_command(command: list[str]) -> str:
    """Render a shell-safe command while redacting database credentials."""

    redacted = [
        _redact_database_url(argument) if "://" in argument else argument
        for argument in command
    ]
    return shlex.join(redacted)


def _redact_database_url(value: str) -> str:
    """Hide a URL password from terminal command previews."""

    if "@" not in value or "://" not in value:
        return value
    scheme, remainder = value.split("://", 1)
    credentials, host = remainder.rsplit("@", 1)
    if ":" not in credentials:
        return value
    username, _password = credentials.split(":", 1)
    return f"{scheme}://{username}:***@{host}"


def _timestamp() -> str:
    """Return a filesystem-safe UTC timestamp for formal artifacts."""

    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
