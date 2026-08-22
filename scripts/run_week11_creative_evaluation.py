from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from mc_agent_harness.core.config import settings  # noqa: E402
from mc_agent_harness.db.models import Base  # noqa: E402
from mc_agent_harness.db.session import create_database_engine, create_session_factory  # noqa: E402
from mc_agent_harness.evaluation.creative import (  # noqa: E402
    CreativeTaskEvaluator,
    FrameArtifact,
    creative_inconclusive_result,
)
from mc_agent_harness.evaluation.mineclip import MineClipScorer  # noqa: E402
from mc_agent_harness.evaluation.video import (  # noqa: E402
    discover_frame_artifacts,
    extract_video_frames,
)
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder  # noqa: E402
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse standalone creative evaluation options."""

    parser = argparse.ArgumentParser(
        description="Score a completed MineDojo creative-task video or frame directory with MineCLIP."
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=ROOT / "tasks" / "executable" / "minedojo_creative_tasks.jsonl",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video", type=Path)
    source.add_argument("--frames-dir", type=Path)
    parser.add_argument("--scorer-url", default=settings.mineclip_scorer_url)
    parser.add_argument("--scorer-timeout-sec", type=float, default=120.0)
    parser.add_argument("--calibration-file", type=Path, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--sample-fps", type=float, default=None)
    parser.add_argument("--max-frames", type=int, default=4096)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--source-validation-report",
        type=Path,
        default=None,
        help=(
            "Optional live_training.json whose trusted-window recording validation must pass "
            "before MineCLIP is invoked."
        ),
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist evaluation events and the query-friendly result to DATABASE_URL.",
    )
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--database-url", default=None)
    database.add_argument("--database-path", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict[str, Any]:
    """Load task metadata, normalize frames, call MineCLIP, and write an audit report."""

    provider = MineDojoTaskProvider(args.manifest_path)
    task_spec = await provider.load_task(args.task_id)
    if task_spec.get("category") != "creative":
        raise ValueError(f"Task is not creative: {args.task_id}.")
    output_dir = args.output_dir or _default_output_dir(args.task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    frame_dir = output_dir / "frames"
    calibration = _load_calibration(args.calibration_file)
    if args.threshold is not None:
        calibration[args.task_id] = {
            **calibration.get(args.task_id, {}),
            "status": "calibrated",
            "score_threshold": args.threshold,
            "method": "explicit_cli_override",
        }
    run_id = args.run_id or f"creative_eval_{uuid.uuid4().hex[:16]}"
    source_validation = _load_source_validation(args.source_validation_report)
    source = {
        "type": "video" if args.video is not None else "frames",
        "path": str((args.video or args.frames_dir).expanduser().resolve()),
        "validation": source_validation,
    }
    recorder = (
        PersistentEvaluationRecorder(
            session_factory=_session_factory(_database_url(args)),
            task_id=args.task_id,
            agent_id="external-mineclip-evaluator",
            worker_id="mineclip-scorer",
        )
        if args.persist
        else None
    )
    if recorder is not None and args.run_id is None:
        await recorder.record(
            run_id,
            "run_started",
            {
                "task_id": args.task_id,
                "task_spec": task_spec,
                "mode": "external_creative_evaluation",
            },
        )
    if source_validation is not None and not _source_validation_is_acceptable(source_validation):
        reasons = source_validation.get("reasons")
        result = creative_inconclusive_result(
            task_spec,
            reason=(
                "MineCLIP was skipped because the Minecraft recording source failed validation: "
                f"{reasons or 'untrusted Minecraft window'}."
            ),
            calibration=calibration.get(args.task_id),
            source_validation=source_validation,
            evidence_source=source,
        )
        if recorder is not None:
            await recorder.record(run_id, "creative_evaluation_inconclusive", result)
    else:
        verifier = (
            task_spec.get("verifier")
            if isinstance(task_spec.get("verifier"), dict)
            else {}
        )
        frame_policy = (
            verifier.get("frame_sampling")
            if isinstance(verifier.get("frame_sampling"), dict)
            else {}
        )
        sample_fps = args.sample_fps or float(frame_policy.get("sample_fps", 2.0))
        if args.video is not None:
            frames = extract_video_frames(
                args.video,
                frame_dir,
                sample_fps=sample_fps,
                max_frames=args.max_frames,
            )
        else:
            assert args.frames_dir is not None
            frames = _copy_frame_artifacts(args.frames_dir, frame_dir, args.max_frames)
        evaluator = CreativeTaskEvaluator(
            MineClipScorer(args.scorer_url, timeout_sec=args.scorer_timeout_sec),
            recorder=recorder,
            calibration_registry=calibration,
        )
        result = await evaluator.evaluate(
            task_spec,
            frames,
            run_id=run_id,
            evidence_source=source,
        )
    if recorder is not None:
        if args.run_id is None:
            await recorder.record(
                run_id,
                "run_finished",
                {
                    "task_id": args.task_id,
                    "steps": 0,
                    "terminated": True,
                    "stop_reason": "external_creative_evaluation",
                },
            )
        await recorder.record(
            run_id,
            "verifier_result",
            {
                "task_id": args.task_id,
                "success": bool(result.get("success")),
                "verifier": result,
                "source": "mineclip_external_evaluator",
                "authoritative": False,
            },
        )
    report = {
        "schema_version": "mc-agent-harness.creative-evaluation-report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "task_id": args.task_id,
        "source": source,
        "scorer_url": args.scorer_url,
        "result": result,
    }
    report_path = output_dir / "creative_evaluation.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "task_id": args.task_id,
        "success": result.get("success"),
        "inconclusive": result.get("inconclusive"),
        "score": result.get("score"),
        "score_threshold": result.get("score_threshold"),
        "frame_count": result.get("frame_count"),
        "window_count": result.get("window_count"),
        "report": str(report_path),
        "persisted": args.persist,
    }


def _load_source_validation(path: Path | None) -> dict[str, Any] | None:
    """Read the bounded recording-validation section from one live report."""

    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    recording = payload.get("recording") if isinstance(payload, dict) else None
    validation = recording.get("validation") if isinstance(recording, dict) else None
    if not isinstance(validation, dict):
        return {
            "valid": False,
            "trusted_minecraft_window": False,
            "reasons": ["recording_validation_missing"],
        }
    return validation


def _source_validation_is_acceptable(validation: dict[str, Any]) -> bool:
    """Require both a decodable video and an explicitly trusted Minecraft window."""

    return bool(
        validation.get("valid") is True
        and validation.get("trusted_minecraft_window") is True
    )


def _copy_frame_artifacts(source: Path, output: Path, max_frames: int) -> list[FrameArtifact]:
    """Copy user-supplied frames under ARTIFACT_ROOT so the dashboard can serve them safely."""

    discovered = discover_frame_artifacts(source)[:max_frames]
    output.mkdir(parents=True, exist_ok=True)
    for existing in output.iterdir():
        if existing.is_file():
            existing.unlink()
    copied: list[FrameArtifact] = []
    for index, frame in enumerate(discovered):
        destination = output / f"frame_{index + 1:06d}{frame.path.suffix.lower()}"
        shutil.copy2(frame.path, destination)
        copied.append(FrameArtifact(path=destination.resolve(), sequence=index))
    return copied


def _load_calibration(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load a reviewed task-id to threshold registry."""

    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Calibration file must contain an object keyed by task_id.")
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _default_output_dir(task_id: str) -> Path:
    """Build a timestamped artifact path below the configured safe root."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_task_id = task_id.replace(":", "_").replace("/", "_")
    return Path(settings.artifact_root) / "week11" / f"{safe_task_id}_{timestamp}"


def _database_url(args: argparse.Namespace) -> str:
    """Resolve an explicit evaluation database before falling back to application settings."""

    if args.database_url:
        return str(args.database_url)
    if args.database_path:
        return f"sqlite+pysqlite:///{args.database_path.expanduser().resolve().as_posix()}"
    return settings.database_url


def _session_factory(database_url: str):
    """Create the selected audit schema and return a recorder-compatible session factory."""

    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def main() -> None:
    """Run creative evaluation and print its compact machine-readable summary."""

    summary = asyncio.run(run(parse_args()))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
