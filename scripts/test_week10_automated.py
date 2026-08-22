from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASK_IDS = ("minedojo_harvest_oak_log",)


@dataclass(frozen=True, slots=True)
class StepResult:
    """Saved result metadata for one automated test step."""

    name: str
    command: list[str]
    cwd: str
    return_code: int
    duration_sec: float
    log_path: str
    status: str
    artifacts: dict[str, str] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    """Parse command-line options for the Week 10 automated test runner."""

    parser = argparse.ArgumentParser(
        description="Run Week 10 static, prompt, benchmark, and optional live Minecraft tests."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory where all test artifacts will be written.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=None,
        help="Task id used for prompt and live tests; repeat to select multiple tasks.",
    )
    parser.add_argument("--manifest-dir", type=Path, default=ROOT / "tasks" / "manifests")
    parser.add_argument("--skip-static", action="store_true", help="Skip typecheck, pytest, and schema checks.")
    parser.add_argument("--skip-prompts", action="store_true", help="Skip prompt dump artifacts.")
    parser.add_argument("--skip-benchmark", action="store_true", help="Skip deterministic benchmark.")
    parser.add_argument(
        "--benchmark-selected-only",
        action="store_true",
        help="Run deterministic benchmark only for --task-id values instead of all curated manifests.",
    )
    parser.add_argument("--live-port", type=int, default=None, help="Minecraft LAN/server port for live tests.")
    parser.add_argument("--live-host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument("--live-scripted", action="store_true", help="Run live scripted Minecraft smoke test.")
    parser.add_argument("--live-llm", action="store_true", help="Run live LLM Minecraft test.")
    parser.add_argument("--model", default=None, help="Optional model id for verify_llm_model.py.")
    parser.add_argument("--worker-concurrency", type=int, default=1)
    parser.add_argument("--start-delay-sec", type=float, default=30.0)
    parser.add_argument("--scripted-max-steps", type=int, default=5)
    parser.add_argument("--llm-max-steps", type=int, default=8)
    parser.add_argument("--spawn-timeout-ms", type=int, default=20000)
    parser.add_argument("--auto-promote", action="store_true")
    parser.add_argument(
        "--clear-inventory-on-reset",
        action="store_true",
        help="Clear verifier target items during live worker reset.",
    )
    parser.add_argument(
        "--clear-item",
        action="append",
        default=None,
        help="Specific item id to clear on live reset; repeat for multiple items.",
    )
    parser.add_argument(
        "--clear-all-inventory-on-reset",
        action="store_true",
        help="Clear the full live worker inventory during reset.",
    )
    parser.add_argument("--clear-inventory-wait-ms", type=int, default=750)
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing step. Artifacts from completed steps are still kept.",
    )
    return parser.parse_args()


def main() -> None:
    """Run the automation and write durable summary artifacts."""

    args = parse_args()
    task_ids = tuple(args.task_id or DEFAULT_TASK_IDS)
    output_dir = args.output_dir or ROOT / "runs" / f"week10_automated_{_timestamp()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "logs").mkdir(exist_ok=True)
    (output_dir / "prompts").mkdir(exist_ok=True)

    env = _test_env()
    steps: list[StepResult] = []
    _write_json(
        output_dir / "metadata.json",
        {
            "created_at": datetime.now(tz=UTC).isoformat(),
            "root": str(ROOT),
            "task_ids": list(task_ids),
            "args": _json_safe(vars(args)),
            "git_status": _capture_git_status(),
        },
    )

    print(f"Week 10 automated test artifacts: {output_dir}")
    if not args.skip_static:
        manifest_report = output_dir / "minedojo_executable_manifest_dry_run.json"
        _append_step(
            steps,
            _run_step(
                "minedojo_executable_manifest_dry_run",
                [
                    _python_bin(),
                    "scripts/build_minedojo_executable_manifests.py",
                    "--pretty",
                ],
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "00_minedojo_executable_manifest_dry_run.log",
                stdout_artifact=manifest_report,
                artifacts={"manifest_report_json": str(manifest_report)},
            ),
            fail_fast=args.fail_fast,
        )
        _append_step(
            steps,
            _run_step(
                "worker_typecheck",
                ["npm", "run", "typecheck"],
                cwd=ROOT / "workers" / "mineflayer-worker",
                env=env,
                log_path=output_dir / "logs" / "01_worker_typecheck.log",
            ),
            fail_fast=args.fail_fast,
        )
        _append_step(
            steps,
            _run_step(
                "pytest_week10_core",
                [
                    _python_bin(),
                    "-m",
                    "pytest",
                    "backend/tests/unit/test_context_manager.py",
                    "backend/tests/unit/test_tool_registry.py",
                    "backend/tests/unit/test_week10_catalog_and_similarity.py",
                    "backend/tests/unit/test_minedojo_adapter.py",
                    "backend/tests/unit/test_programmatic_verifiers.py",
                    "backend/tests/unit/test_server_command_reset.py",
                    "backend/tests/unit/test_server_pool.py",
                    "backend/tests/unit/test_week10_live_training.py",
                    "backend/tests/unit/test_live_training_cli.py",
                    "backend/tests/unit/test_week10_formal_batch_cli.py",
                    "backend/tests/unit/test_skill_library.py",
                    "-q",
                ],
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "02_pytest_week10_core.log",
            ),
            fail_fast=args.fail_fast,
        )
        _append_step(
            steps,
            _run_step(
                "validate_json_schemas",
                [_python_bin(), "scripts/validate_json_schemas.py"],
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "03_validate_json_schemas.log",
            ),
            fail_fast=args.fail_fast,
        )

    if not args.skip_prompts:
        for index, task_id in enumerate(task_ids, start=1):
            prompt_path = output_dir / "prompts" / f"{task_id}.prompt.json"
            _append_step(
                steps,
                _run_step(
                    f"dump_prompt_{task_id}",
                    [
                        _python_bin(),
                        "scripts/dump_agent_prompt.py",
                        "--task-id",
                        task_id,
                        "--manifest-dir",
                        str(args.manifest_dir),
                        "--pretty",
                    ],
                    cwd=ROOT,
                    env=env,
                    log_path=output_dir / "logs" / f"04_{index:02d}_dump_prompt_{task_id}.log",
                    stdout_artifact=prompt_path,
                    artifacts={"prompt_json": str(prompt_path)},
                ),
                fail_fast=args.fail_fast,
            )

    if not args.skip_benchmark:
        benchmark_dir = output_dir / "benchmark"
        command = [
            _python_bin(),
            "scripts/run_week6_benchmark.py",
            "--manifest-dir",
            str(args.manifest_dir),
            "--output-dir",
            str(benchmark_dir),
        ]
        if args.benchmark_selected_only:
            for task_id in task_ids:
                command.extend(["--task-id", task_id])
        _append_step(
            steps,
            _run_step(
                "deterministic_benchmark",
                command,
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "05_deterministic_benchmark.log",
                artifacts={"benchmark_dir": str(benchmark_dir)},
            ),
            fail_fast=args.fail_fast,
        )

    if args.live_scripted or args.live_llm:
        if args.live_port is None:
            raise SystemExit("--live-port is required when --live-scripted or --live-llm is set.")

    if args.live_scripted:
        live_dir = output_dir / "live_scripted"
        live_dir.mkdir(exist_ok=True)
        _append_step(
            steps,
            _run_step(
                "live_scripted_training",
                _live_command(
                    args,
                    task_ids,
                    live_dir,
                    scripted=True,
                    max_steps=args.scripted_max_steps,
                ),
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "06_live_scripted_training.log",
                artifacts={
                    "live_json": str(live_dir / "week10_live_training.json"),
                    "live_sqlite": str(live_dir / "week10_live_training.sqlite3"),
                },
            ),
            fail_fast=args.fail_fast,
        )

    if args.live_llm:
        model_dir = output_dir / "model"
        model_dir.mkdir(exist_ok=True)
        verify_command = [_python_bin(), "scripts/verify_llm_model.py", "--output", str(model_dir / "verify_llm.json")]
        if args.model:
            verify_command.extend(["--model", args.model])
        _append_step(
            steps,
            _run_step(
                "verify_llm_model",
                verify_command,
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "07_verify_llm_model.log",
                artifacts={"verify_llm_json": str(model_dir / "verify_llm.json")},
            ),
            fail_fast=args.fail_fast,
        )
        live_dir = output_dir / "live_llm"
        live_dir.mkdir(exist_ok=True)
        _append_step(
            steps,
            _run_step(
                "live_llm_training",
                _live_command(
                    args,
                    task_ids,
                    live_dir,
                    scripted=False,
                    max_steps=args.llm_max_steps,
                ),
                cwd=ROOT,
                env=env,
                log_path=output_dir / "logs" / "08_live_llm_training.log",
                artifacts={
                    "live_json": str(live_dir / "week10_live_training.json"),
                    "live_sqlite": str(live_dir / "week10_live_training.sqlite3"),
                },
            ),
            fail_fast=args.fail_fast,
        )

    summary = _summary(output_dir, steps)
    _write_json(output_dir / "summary.json", summary)
    (output_dir / "summary.md").write_text(_summary_markdown(summary), encoding="utf-8")
    print(json.dumps({"status": summary["status"], "output_dir": str(output_dir)}, indent=2, sort_keys=True))
    if summary["status"] != "passed":
        raise SystemExit(1)


def _run_step(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    stdout_artifact: Path | None = None,
    artifacts: dict[str, str] | None = None,
) -> StepResult:
    """Run one command, save its output, and return structured metadata."""

    started = time.perf_counter()
    print(f"[week10] {name} ...")
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    duration = time.perf_counter() - started
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(completed.stdout, encoding="utf-8")
    if stdout_artifact is not None and completed.returncode == 0:
        stdout_artifact.parent.mkdir(parents=True, exist_ok=True)
        stdout_artifact.write_text(completed.stdout, encoding="utf-8")
    status = "passed" if completed.returncode == 0 else "failed"
    print(f"[week10] {name}: {status} ({duration:.2f}s)")
    return StepResult(
        name=name,
        command=command,
        cwd=str(cwd),
        return_code=completed.returncode,
        duration_sec=duration,
        log_path=str(log_path),
        status=status,
        artifacts=artifacts or {},
    )


def _append_step(steps: list[StepResult], step: StepResult, *, fail_fast: bool) -> None:
    """Append a step and optionally stop immediately on failure."""

    steps.append(step)
    if fail_fast and step.status != "passed":
        raise SystemExit(f"Step failed: {step.name}. See {step.log_path}")


def _live_command(
    args: argparse.Namespace,
    task_ids: tuple[str, ...],
    live_dir: Path,
    *,
    scripted: bool,
    max_steps: int,
) -> list[str]:
    """Build a run_week10_live_training.py command with durable output paths."""

    command = [
        _python_bin(),
        "scripts/run_week10_live_training.py",
        "--host",
        args.live_host,
        "--port",
        str(args.live_port),
        "--manifest-dir",
        str(args.manifest_dir),
        "--worker-concurrency",
        str(min(args.worker_concurrency, len(task_ids))),
        "--spawn-timeout-ms",
        str(args.spawn_timeout_ms),
        "--start-delay-sec",
        str(args.start_delay_sec),
        "--max-steps-per-task",
        str(max_steps),
        "--database-path",
        str(live_dir / "week10_live_training.sqlite3"),
        "--output",
        str(live_dir / "week10_live_training.json"),
    ]
    for task_id in task_ids:
        command.extend(["--task-id", task_id])
    if scripted:
        command.append("--scripted")
    if args.auto_promote:
        command.append("--auto-promote")
    if args.clear_inventory_on_reset:
        command.append("--clear-inventory-on-reset")
    if args.clear_all_inventory_on_reset:
        command.append("--clear-all-inventory-on-reset")
    if args.clear_inventory_wait_ms:
        command.extend(["--clear-inventory-wait-ms", str(args.clear_inventory_wait_ms)])
    for item in args.clear_item or []:
        command.extend(["--clear-item", item])
    return command


def _summary(output_dir: Path, steps: list[StepResult]) -> dict[str, Any]:
    """Build the final machine-readable summary payload."""

    failed = [step for step in steps if step.status != "passed"]
    return {
        "status": "passed" if not failed else "failed",
        "output_dir": str(output_dir),
        "step_count": len(steps),
        "failed_steps": [step.name for step in failed],
        "steps": [asdict(step) for step in steps],
    }


def _summary_markdown(summary: dict[str, Any]) -> str:
    """Render a human-readable Markdown summary for the saved test run."""

    lines = [
        "# Week 10 Automated Test Summary",
        "",
        f"- Status: `{summary['status']}`",
        f"- Output dir: `{summary['output_dir']}`",
        f"- Steps: {summary['step_count']}",
        f"- Failed steps: {', '.join(summary['failed_steps']) if summary['failed_steps'] else 'none'}",
        "",
        "| Step | Status | Seconds | Log |",
        "| --- | --- | ---: | --- |",
    ]
    for step in summary["steps"]:
        lines.append(
            f"| `{step['name']}` | `{step['status']}` | {step['duration_sec']:.2f} | `{step['log_path']}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _test_env() -> dict[str, str]:
    """Return environment variables used by subprocess test commands."""

    env = os.environ.copy()
    backend_src = str(ROOT / "backend" / "src")
    env["PYTHONPATH"] = backend_src if not env.get("PYTHONPATH") else f"{backend_src}{os.pathsep}{env['PYTHONPATH']}"
    return env


def _python_bin() -> str:
    """Return the preferred Python interpreter for project scripts."""

    configured = os.getenv("PYTHON")
    if configured:
        return configured
    venv_python = ROOT / "backend" / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def _capture_git_status() -> str:
    """Capture current git status for reproducibility metadata."""

    completed = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return completed.stdout


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write stable pretty JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _json_safe(value: Any) -> Any:
    """Convert common argparse values into JSON-safe payloads."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _timestamp() -> str:
    """Return a UTC timestamp suitable for artifact directory names."""

    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
