from __future__ import annotations

import argparse
import json
import os
import random
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"


def parse_args() -> argparse.Namespace:
    """Parse options for one-shot random live MineDojo training."""

    parser = argparse.ArgumentParser(
        description=(
            "Build executable MineDojo manifests, randomly sample tasks, and run live "
            "Minecraft LLM training with skill promotion enabled."
        )
    )
    parser.add_argument("--category", default="combat", help="Task category to sample from.")
    parser.add_argument("--count", type=int, default=5, help="Number of tasks to sample.")
    parser.add_argument("--seed", type=int, default=None, help="Optional deterministic sampling seed.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for manifests, logs, JSON, and SQLite.")
    parser.add_argument("--host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MINECRAFT_PORT", "25565")))
    parser.add_argument("--worker-concurrency", type=int, default=1)
    parser.add_argument("--rcon-host", default=os.getenv("MINECRAFT_RCON_HOST"))
    parser.add_argument("--rcon-port", type=int, default=int(os.getenv("MINECRAFT_RCON_PORT", "25575")))
    parser.add_argument("--rcon-password", default=os.getenv("MINECRAFT_RCON_PASSWORD"))
    parser.add_argument("--max-steps-per-task", type=int, default=20)
    parser.add_argument("--max-runtime-sec-per-task", type=float, default=300)
    parser.add_argument("--start-delay-sec", type=float, default=10)
    parser.add_argument("--rcon-set-time", default="night")
    parser.add_argument("--rcon-set-weather", default="clear")
    parser.add_argument("--no-clear-all-inventory-on-reset", action="store_true")
    parser.add_argument("--no-auto-promote", action="store_true")
    parser.add_argument("--model-timeout-retries", type=int, default=2)
    parser.add_argument("--model-timeout-requeues", type=int, default=1)
    parser.add_argument(
        "--no-official-specs",
        action="store_true",
        help="Build executable manifests without downloading MineDojo official task specs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and sample manifests, then print the live command without executing it.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the random live training pipeline."""

    args = parse_args()
    if args.count < 1:
        raise SystemExit("--count must be >= 1.")
    if not args.rcon_password and not args.dry_run:
        raise SystemExit("Set MINECRAFT_RCON_PASSWORD or pass --rcon-password.")

    output_dir = args.output_dir or ROOT / "runs" / f"random_{args.category}{args.count}_{_timestamp()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "minedojo_executable_manifests.jsonl"
    summary_path = output_dir / "minedojo_executable_manifests.summary.json"
    selected_path = output_dir / "selected_tasks.json"
    live_output_path = output_dir / f"live_random_{args.category}{args.count}.json"
    database_path = output_dir / f"live_random_{args.category}{args.count}.sqlite3"

    build_command = _build_manifest_command(args, manifest_path, summary_path)
    _run(build_command, cwd=ROOT, env=_env())

    selected_tasks = _sample_tasks(
        manifest_path=manifest_path,
        category=args.category,
        count=args.count,
        seed=args.seed,
    )
    selected_path.write_text(
        json.dumps(
            {
                "category": args.category,
                "count": args.count,
                "seed": args.seed,
                "task_ids": selected_tasks,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    live_command = _live_training_command(
        args=args,
        manifest_path=manifest_path,
        selected_tasks=selected_tasks,
        live_output_path=live_output_path,
        database_path=database_path,
    )
    print("Selected task ids:")
    for task_id in selected_tasks:
        print(f"  - {task_id}")
    print("\nLive command:")
    print(_shell_command(live_command))
    print(f"\nArtifacts directory: {output_dir}")
    if args.dry_run:
        return
    _run(live_command, cwd=ROOT, env=_env())


def _build_manifest_command(args: argparse.Namespace, manifest_path: Path, summary_path: Path) -> list[str]:
    """Build the executable manifest generation command."""

    command = [
        _python_bin(),
        "scripts/build_minedojo_executable_manifests.py",
        "--output-jsonl",
        str(manifest_path),
        "--summary-path",
        str(summary_path),
        "--pretty",
    ]
    if args.no_official_specs:
        command.append("--no-official-specs")
    return command


def _live_training_command(
    *,
    args: argparse.Namespace,
    manifest_path: Path,
    selected_tasks: list[str],
    live_output_path: Path,
    database_path: Path,
) -> list[str]:
    """Build the live training command with sampled task ids."""

    command = [
        _python_bin(),
        "scripts/run_week10_live_training.py",
        "--manifest-dir",
        str(manifest_path),
    ]
    for task_id in selected_tasks:
        command.extend(["--task-id", task_id])
    command.extend(
        [
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--worker-concurrency",
            str(args.worker_concurrency),
            "--rcon-reset",
            "--rcon-port",
            str(args.rcon_port),
            "--rcon-password",
            str(args.rcon_password or "$MINECRAFT_RCON_PASSWORD"),
            "--max-steps-per-task",
            str(args.max_steps_per_task),
            "--max-runtime-sec-per-task",
            str(args.max_runtime_sec_per_task),
            "--start-delay-sec",
            str(args.start_delay_sec),
            "--model-timeout-retries",
            str(args.model_timeout_retries),
            "--model-timeout-requeues",
            str(args.model_timeout_requeues),
            "--output",
            str(live_output_path),
            "--database-path",
            str(database_path),
        ]
    )
    if args.rcon_host:
        command.extend(["--rcon-host", args.rcon_host])
    if args.rcon_set_time:
        command.extend(["--rcon-set-time", args.rcon_set_time])
    if args.rcon_set_weather:
        command.extend(["--rcon-set-weather", args.rcon_set_weather])
    if not args.no_clear_all_inventory_on_reset:
        command.append("--clear-all-inventory-on-reset")
    if not args.no_auto_promote:
        command.append("--auto-promote")
    return command


def _sample_tasks(manifest_path: Path, category: str, count: int, seed: int | None) -> list[str]:
    """Randomly sample task ids from one executable manifest JSONL file."""

    tasks: list[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict) and payload.get("category") == category and payload.get("task_id"):
            tasks.append(str(payload["task_id"]))
    if len(tasks) < count:
        raise SystemExit(f"Only found {len(tasks)} executable {category} tasks; requested {count}.")
    rng = random.Random(seed)
    return rng.sample(tasks, count)


def _run(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run one subprocess and stream output to the current terminal."""

    print(f"\n$ {_shell_command(command)}\n")
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _env() -> dict[str, str]:
    """Build a child environment with backend imports enabled."""

    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{BACKEND_SRC}{os.pathsep}{existing}" if existing else str(BACKEND_SRC)
    return env


def _python_bin() -> str:
    """Return the project Python interpreter path."""

    return str(ROOT / "backend" / ".venv" / "bin" / "python")


def _shell_command(command: list[str]) -> str:
    """Render a shell-copyable command."""

    return shlex.join(command)


def _timestamp() -> str:
    """Return a UTC timestamp for run artifact directories."""

    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
