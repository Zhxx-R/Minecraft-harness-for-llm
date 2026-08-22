from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl"


def parse_args() -> argparse.Namespace:
    """Parse the end-to-end creative execution, recording, and evaluation options."""

    parser = argparse.ArgumentParser(
        description=(
            "Run one authentic MineDojo creative task, record the agent POV, score it with "
            "MineCLIP, and persist the result against the same run."
        )
    )
    parser.add_argument(
        "--task-id", default=None, help="Defaults to a seeded random creative task."
    )
    parser.add_argument("--seed", type=int, default=1111)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--host", default=os.getenv("MINECRAFT_HOST", "localhost"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("MINECRAFT_PORT", "25565"))
    )
    parser.add_argument("--rcon-reset", action="store_true")
    parser.add_argument(
        "--rcon-host", default=os.getenv("MINECRAFT_RCON_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--rcon-port", type=int, default=int(os.getenv("MINECRAFT_RCON_PORT", "25575"))
    )
    parser.add_argument("--rcon-password", default=os.getenv("MINECRAFT_RCON_PASSWORD"))
    parser.add_argument("--random-teleport", action="store_true")
    parser.add_argument("--threat-pause", action="store_true")
    parser.add_argument(
        "--spectator-player", default=os.getenv("MC_AGENT_SPECTATOR_PLAYER")
    )
    parser.add_argument(
        "--recording-window-title",
        default=os.getenv("MC_AGENT_RECORDING_WINDOW_TITLE", "Minecraft"),
    )
    parser.add_argument(
        "--recording-window-owner", default=os.getenv("MC_AGENT_RECORDING_WINDOW_OWNER")
    )
    parser.add_argument(
        "--recording-input",
        default=os.getenv("MC_AGENT_RECORDING_INPUT", "Capture screen 0:none"),
    )
    parser.add_argument(
        "--recording-filter", default=os.getenv("MC_AGENT_RECORDING_FILTER")
    )
    parser.add_argument(
        "--agent-visual-snapshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Make request_visual_snapshot inject the trusted client frame into Qwen's next turn.",
    )
    parser.add_argument(
        "--initial-visual-snapshot",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Inject one trusted post-reset agent POV frame into the first Qwen turn.",
    )
    parser.add_argument(
        "--mineclip-progress-feedback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Return asynchronous advisory MineCLIP trends after important world-changing actions."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--max-runtime-sec", type=float, default=1800.0)
    parser.add_argument(
        "--scorer-url",
        default=os.getenv("MINECLIP_SCORER_URL", "http://127.0.0.1:8091"),
    )
    parser.add_argument(
        "--skip-scorer-preflight",
        action="store_true",
        help="Record the live task even when MineCLIP readiness cannot be checked first.",
    )
    parser.add_argument(
        "--manage-local-scorer",
        action="store_true",
        help=(
            "Keep the project-managed MineCLIP process stopped during Minecraft execution, "
            "then start it for offline scoring and stop it afterward."
        ),
    )
    parser.add_argument(
        "--keep-local-scorer",
        action="store_true",
        help="Keep a scorer started by --manage-local-scorer running after evaluation.",
    )
    parser.add_argument("--calibration-file", type=Path, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    database.add_argument("--database-path", type=Path, default=None)
    parser.add_argument(
        "live_extra_args",
        nargs=argparse.REMAINDER,
        help="Additional run_week10_live_training.py arguments after --.",
    )
    return parser.parse_args()


def main() -> None:
    """Execute the live runner and external evaluator as one auditable workflow."""

    args = parse_args()
    if args.mineclip_progress_feedback and args.manage_local_scorer:
        raise SystemExit(
            "--mineclip-progress-feedback needs MineCLIP during live execution and cannot be "
            "combined with --manage-local-scorer, which intentionally starts scoring afterward. "
            "Start scripts/mineclip_scorer.sh first or use run_week11_local_creative.sh."
        )
    task_id = args.task_id or _random_task_id(args.manifest_path, args.seed)
    scorer_health: dict[str, Any] | None = None
    if args.manage_local_scorer:
        _run_local_scorer_control("stop")
        if _is_scorer_ready(args.scorer_url):
            raise RuntimeError(
                "A MineCLIP service remains active outside project process management; stop it "
                "before using --manage-local-scorer."
            )
    elif not args.skip_scorer_preflight:
        scorer_health = _require_scorer_ready(args.scorer_url)
        print(
            json.dumps(
                {"phase": "mineclip_preflight", "health": scorer_health}, indent=2
            )
        )
    output_dir = args.output_dir or _default_output_dir(task_id)
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    live_report = output_dir / "live_training.json"
    database_path = (
        args.database_path.expanduser().resolve()
        if args.database_path is not None
        else output_dir / "audit.sqlite3"
    )
    video_path = output_dir / "agent_pov.mp4"
    evaluation_dir = output_dir / "evaluation"

    live_command = [
        sys.executable,
        str(ROOT / "scripts" / "run_week10_live_training.py"),
        "--manifest-dir",
        str(args.manifest_path.expanduser().resolve()),
        "--task-id",
        task_id,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--worker-concurrency",
        "1",
        "--max-steps-per-task",
        str(args.max_steps),
        "--max-runtime-sec-per-task",
        str(args.max_runtime_sec),
        "--output",
        str(live_report),
        "--record-agent-video",
        "--recording-output",
        str(video_path),
        "--recording-input",
        args.recording_input,
    ]
    if args.database_url:
        live_command.extend(["--database-url", str(args.database_url)])
    else:
        live_command.extend(["--database-path", str(database_path)])
    _append_live_flags(live_command, args)
    print(
        json.dumps(
            {
                "phase": "live_execution",
                "task_id": task_id,
                "command": _redacted_command(live_command),
            },
            indent=2,
        )
    )
    live_environment = (
        {"MINECRAFT_RCON_PASSWORD": str(args.rcon_password)}
        if args.rcon_password
        else None
    )
    completed = _run_command(live_command, environment_overrides=live_environment)
    if completed.returncode != 0 and not live_report.is_file():
        raise SystemExit(completed.returncode)

    live_payload = json.loads(live_report.read_text(encoding="utf-8"))
    run_id = _first_run_id(live_payload)
    database_url = str(live_payload.get("database_url") or "")
    if not database_url:
        raise RuntimeError("Live report did not include database_url.")
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise RuntimeError(f"Agent POV recording is missing or empty: {video_path}")
    recording_validation = _recording_validation(live_payload)
    capture_acceptable = _source_validation_is_acceptable(recording_validation)

    evaluation_command = [
        sys.executable,
        str(ROOT / "scripts" / "run_week11_creative_evaluation.py"),
        "--manifest-path",
        str(args.manifest_path.expanduser().resolve()),
        "--task-id",
        task_id,
        "--video",
        str(video_path),
        "--scorer-url",
        args.scorer_url,
        "--run-id",
        run_id,
        "--source-validation-report",
        str(live_report),
        "--persist",
        "--database-url",
        database_url,
        "--output-dir",
        str(evaluation_dir),
    ]
    if args.calibration_file is not None:
        evaluation_command.extend(
            ["--calibration-file", str(args.calibration_file.expanduser().resolve())]
        )
    if args.threshold is not None:
        evaluation_command.extend(["--threshold", str(args.threshold)])
    manage_scorer = bool(args.manage_local_scorer)
    scorer_started = False
    try:
        if manage_scorer and capture_acceptable:
            _run_local_scorer_control("start")
            scorer_started = True
            scorer_health = _require_scorer_ready(args.scorer_url)
            print(
                json.dumps(
                    {"phase": "offline_mineclip_started", "health": scorer_health},
                    indent=2,
                )
            )
        phase = (
            "mineclip_evaluation"
            if capture_acceptable
            else "mineclip_skipped_invalid_capture"
        )
        print(
            json.dumps(
                {
                    "phase": phase,
                    "run_id": run_id,
                    "recording_validation": recording_validation,
                    "command": _redacted_command(evaluation_command),
                },
                indent=2,
            )
        )
        evaluated = _run_command(evaluation_command)
        if evaluated.returncode != 0:
            raise SystemExit(evaluated.returncode)
    finally:
        if scorer_started and not args.keep_local_scorer:
            _run_local_scorer_control("stop")

    summary = {
        "schema_version": "mc-agent-harness.week11-creative-workflow.v1",
        "task_id": task_id,
        "run_id": run_id,
        "live_report": str(live_report),
        "database_url": _redacted_database_url(database_url),
        "recording": str(video_path),
        "recording_validation": recording_validation,
        "evaluation_report": str(evaluation_dir / "creative_evaluation.json"),
        "mineclip": {
            "mode": "managed_offline" if manage_scorer else "external_service",
            "health": scorer_health,
            "skipped_due_to_invalid_capture": not capture_acceptable,
            "kept_running": bool(scorer_started and args.keep_local_scorer),
        },
    }
    summary_path = output_dir / "workflow_summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _random_task_id(manifest_path: Path, seed: int) -> str:
    """Select one reproducible creative task from the executable snapshot."""

    task_ids = [
        str(payload["task_id"])
        for payload in (
            json.loads(line)
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if payload.get("category") == "creative"
    ]
    if not task_ids:
        raise ValueError(f"No creative tasks found in {manifest_path}.")
    return random.Random(seed).choice(sorted(task_ids))


def _default_output_dir(task_id: str) -> Path:
    """Build a timestamped workflow artifact directory below runs/week11."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_task_id = task_id.replace(":", "_").replace("/", "_")
    return ROOT / "runs" / "week11" / f"{safe_task_id}_{timestamp}"


def _require_scorer_ready(base_url: str) -> dict[str, Any]:
    """Fail before a long live run when the isolated MineCLIP service is not ready."""

    with urlopen(f"{base_url.rstrip('/')}/health", timeout=10) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        raise RuntimeError(f"MineCLIP scorer is not ready: {payload}")
    return payload


def _is_scorer_ready(base_url: str) -> bool:
    """Return whether a scorer responds ready without turning absence into an error."""

    try:
        _require_scorer_ready(base_url)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _local_scorer_command(action: str) -> list[str]:
    """Build the project-local scorer lifecycle command for auditable orchestration."""

    if action not in {"start", "stop", "status", "smoke"}:
        raise ValueError(f"Unsupported local scorer action: {action!r}")
    return [str(ROOT / "scripts" / "mineclip_scorer.sh"), action]


def _run_local_scorer_control(action: str) -> None:
    """Run one project-managed scorer lifecycle transition and fail visibly on errors."""

    completed = subprocess.run(
        _local_scorer_command(action), cwd=ROOT, text=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Local MineCLIP scorer action {action!r} failed.")


def _append_live_flags(command: list[str], args: argparse.Namespace) -> None:
    """Append optional reset, spectator, recording, and pass-through live-run flags."""

    requires_rcon = bool(
        args.rcon_reset
        or args.random_teleport
        or args.threat_pause
        or args.spectator_player
    )
    if requires_rcon and not args.rcon_password:
        raise ValueError(
            "RCON-backed reset, teleport, threat pause, or spectator follow requires "
            "--rcon-password or MINECRAFT_RCON_PASSWORD."
        )
    if requires_rcon:
        command.extend(
            ["--rcon-host", str(args.rcon_host), "--rcon-port", str(args.rcon_port)]
        )
    if args.rcon_reset:
        command.extend(["--rcon-reset", "--clear-all-inventory-on-reset"])
    if args.random_teleport:
        command.append("--rcon-random-teleport-on-reset")
    if args.threat_pause:
        command.append("--threat-pause")
    if args.spectator_player:
        command.extend(["--spectator-player", args.spectator_player])
    if args.recording_window_title:
        command.extend(["--recording-window-title", args.recording_window_title])
    if getattr(args, "recording_window_owner", None):
        command.extend(["--recording-window-owner", args.recording_window_owner])
    if args.recording_filter:
        command.extend(["--recording-filter", args.recording_filter])
    if getattr(args, "agent_visual_snapshots", True):
        command.append("--agent-visual-snapshots")
    else:
        command.append("--no-agent-visual-snapshots")
    if getattr(args, "initial_visual_snapshot", True):
        command.append("--initial-visual-snapshot")
    else:
        command.append("--no-initial-visual-snapshot")
    if getattr(args, "mineclip_progress_feedback", False):
        command.extend(
            [
                "--mineclip-progress-feedback",
                "--mineclip-progress-scorer-url",
                args.scorer_url,
            ]
        )
    extra_args = list(args.live_extra_args)
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    command.extend(extra_args)


def _run_command(
    command: list[str],
    *,
    environment_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one visible child process with the backend source path configured."""

    environment = dict(os.environ)
    environment.update(environment_overrides or {})
    existing = environment.get("PYTHONPATH")
    backend_src = str(ROOT / "backend" / "src")
    environment["PYTHONPATH"] = (
        f"{backend_src}{os.pathsep}{existing}" if existing else backend_src
    )
    return subprocess.run(command, cwd=ROOT, env=environment, text=True, check=False)


def _redacted_command(command: list[str]) -> list[str]:
    """Hide credentials in diagnostic command output while preserving executable arguments."""

    redacted = list(command)
    sensitive_options = {"--database-url", "--rcon-password"}
    for index, value in enumerate(redacted[:-1]):
        if value in sensitive_options:
            redacted[index + 1] = "<redacted>"
    return redacted


def _redacted_database_url(database_url: str) -> str:
    """Mask URL userinfo while retaining enough location metadata for audit summaries."""

    scheme, separator, remainder = database_url.partition("://")
    if not separator:
        return database_url
    authority, slash, path = remainder.partition("/")
    if "@" not in authority:
        return database_url
    userinfo, host = authority.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    suffix = f"/{path}" if slash else ""
    return f"{scheme}://{username}:<redacted>@{host}{suffix}"


def _first_run_id(payload: dict[str, Any]) -> str:
    """Extract the single creative run id from a live training report."""

    outcomes = payload.get("outcomes")
    if (
        not isinstance(outcomes, list)
        or not outcomes
        or not isinstance(outcomes[0], dict)
    ):
        raise ValueError("Live report did not contain a task outcome.")
    run_id = outcomes[0].get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Live task outcome did not contain run_id.")
    return run_id


def _recording_validation(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract recording evidence and convert missing fields into an explicit failure."""

    recording = payload.get("recording") if isinstance(payload, dict) else None
    validation = recording.get("validation") if isinstance(recording, dict) else None
    if isinstance(validation, dict):
        return validation
    return {
        "valid": False,
        "trusted_minecraft_window": False,
        "reasons": ["recording_validation_missing"],
    }


def _source_validation_is_acceptable(validation: dict[str, Any]) -> bool:
    """Allow MineCLIP only for a valid video tied to a trusted Minecraft window."""

    return bool(
        validation.get("valid") is True
        and validation.get("trusted_minecraft_window") is True
    )


if __name__ == "__main__":
    main()
