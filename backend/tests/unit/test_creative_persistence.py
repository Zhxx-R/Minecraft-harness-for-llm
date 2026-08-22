from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    CreativeEvaluationRecord,
    HumanReviewRecord,
    RunRecord,
)
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder


def _session_factory() -> sessionmaker[Session]:
    """Create an isolated SQL store for creative persistence tests."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.mark.anyio
async def test_persistent_recorder_upserts_creative_evaluation() -> None:
    """MineCLIP evidence populates audit tables but leaves success to human review."""

    factory = _session_factory()
    recorder = PersistentEvaluationRecorder(
        factory,
        task_id="creative:test",
        agent_id="external-mineclip-evaluator",
    )
    await recorder.record(
        "creative-run",
        "run_started",
        {"task_id": "creative:test", "task_spec": {"category": "creative"}},
    )
    await recorder.record(
        "creative-run",
        "creative_evaluation_completed",
        {
            "task_id": "creative:test",
            "prompt": "Build a stone tower",
            "success": True,
            "inconclusive": False,
            "score": 0.72,
            "score_threshold": 0.5,
            "frame_count": 24,
            "window_count": 2,
            "calibration": {"status": "calibrated"},
            "score_trend": [
                {
                    "target_probability": 0.72,
                    "scorer": {"name": "mineclip_official", "variant": "attn"},
                }
            ],
            "key_frames": [],
            "final_frame": {"path": "/tmp/final.jpg", "sequence": 23},
            "evidence_source": {"type": "video", "path": "/tmp/agent_pov.mp4"},
        },
    )
    await recorder.record(
        "creative-run",
        "verifier_result",
        {
            "task_id": "creative:test",
            "success": True,
            "authoritative": False,
            "source": "mineclip_external_evaluator",
        },
    )

    with factory() as session:
        record = session.scalar(select(CreativeEvaluationRecord))
        review = session.scalar(select(HumanReviewRecord))
        run = session.get(RunRecord, "creative-run")

    assert record is not None
    assert record.score == 0.72
    assert record.success is True
    assert record.scorer == "mineclip_official"
    assert record.variant == "attn"
    assert record.calibration_status == "calibrated"
    assert review is not None
    assert review.status == "awaiting_review"
    assert review.evidence["source"]["path"] == "/tmp/agent_pov.mp4"
    assert run is not None
    assert run.status == "awaiting_human_review"


@pytest.mark.anyio
async def test_agent_submission_creates_pending_human_review() -> None:
    """Accepted external-evaluation submissions enter review before MineCLIP finishes."""

    factory = _session_factory()
    recorder = PersistentEvaluationRecorder(
        factory,
        task_id="creative:test",
        agent_id="creative-agent",
    )
    await recorder.record(
        "submitted-run",
        "run_started",
        {
            "task_id": "creative:test",
            "task_spec": {
                "task_id": "creative:test",
                "category": "creative",
                "verifier": {
                    "type": "creative_mineclip",
                    "prompt": "Build a compact shelter",
                },
            },
        },
    )
    await recorder.record(
        "submitted-run",
        "agent_finish_accepted",
        {
            "task_id": "creative:test",
            "step_index": 7,
            "accepted": True,
            "stop_reason": "agent_submitted_for_external_evaluation",
            "decision": {
                "reasoning_summary": "The structure has walls, a doorway, and a roof.",
                "evidence": ["placed roof blocks"],
            },
            "verifier": {"inconclusive": True},
            "action_result": {"ok": True},
        },
    )
    await recorder.record(
        "submitted-run",
        "run_finished",
        {
            "task_id": "creative:test",
            "steps": 8,
            "terminated": True,
            "stop_reason": "agent_submitted_for_external_evaluation",
        },
    )

    with factory() as session:
        review = session.scalar(select(HumanReviewRecord))
        run = session.get(RunRecord, "submitted-run")

    assert review is not None
    assert review.task_name == "Build a compact shelter"
    assert review.submission_summary.startswith("The structure has walls")
    assert review.evidence["submission"]["step_index"] == 7
    assert run is not None and run.status == "awaiting_human_review"


@pytest.mark.anyio
async def test_late_mineclip_result_cannot_overwrite_human_decision() -> None:
    """Non-authoritative scorer events may enrich evidence after approval but not change it."""

    factory = _session_factory()
    with factory() as session:
        session.add(
            RunRecord(
                id="approved-run",
                task_id="creative:test",
                status="succeeded",
                task_spec={"task_id": "creative:test", "category": "creative"},
            )
        )
        session.add(
            HumanReviewRecord(
                run_id="approved-run",
                task_id="creative:test",
                task_name="Build a compact shelter",
                status="approved",
                decision="approved",
                reviewer_id="reviewer-1",
                version=2,
            )
        )
        session.commit()
    recorder = PersistentEvaluationRecorder(factory, task_id="creative:test")

    await recorder.record(
        "approved-run",
        "creative_evaluation_completed",
        {
            "task_id": "creative:test",
            "prompt": "Build a compact shelter",
            "success": False,
            "inconclusive": False,
            "score": 0.2,
            "frame_count": 16,
            "window_count": 1,
            "calibration": {"status": "pending"},
            "score_trend": [],
            "key_frames": [],
        },
    )
    await recorder.record(
        "approved-run",
        "verifier_result",
        {
            "task_id": "creative:test",
            "success": False,
            "authoritative": False,
            "source": "mineclip_external_evaluator",
        },
    )

    with factory() as session:
        review = session.scalar(select(HumanReviewRecord))
        run = session.get(RunRecord, "approved-run")

    assert review is not None and review.status == "approved"
    assert review.evidence["mineclip"]["score"] == 0.2
    assert run is not None and run.status == "succeeded"
