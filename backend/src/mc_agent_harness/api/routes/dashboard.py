from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Select, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import FileResponse, StreamingResponse

from mc_agent_harness.api.routes.launcher import require_local_control
from mc_agent_harness.core.config import settings
from mc_agent_harness.db.models import (
    CreativeEvaluationRecord,
    HumanReviewRecord,
    LearningCandidateRecord,
    ModelCallRecord,
    RoundSpanRecord,
    RunRecord,
    RuntimeErrorRecord,
    SKILL_DELETED_STATUS,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.db.session import SessionLocal
from mc_agent_harness.evaluation.comparison import build_week8_comparison
from mc_agent_harness.observability.identity import (
    AuditIdentity,
    enrich_event_payload,
    identity_from_task_spec,
)
from mc_agent_harness.observability.tracing import (
    root_span_id_for_run,
    span_id_for_round,
    trace_id_for_run,
)
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus


router = APIRouter(tags=["dashboard"])

_MODEL_REPLAY_EVENT_TYPES = {
    "model_error",
    "invalid_action",
    "model_repair_attempt",
    "model_repair_failed",
    "model_repair_success",
    "model_fallback_action",
    "model_repair_exhausted",
    "model_action",
}


class RunSummary(BaseModel):
    """Compact run row displayed in the dashboard run list."""

    id: str
    trace_id: str
    root_span_id: str
    task_id: str
    status: str
    lifecycle_status: str
    task_result: str
    verifier_success: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    step_count: int
    event_count: int
    model_call_count: int
    runtime_error_count: int


class RunDetail(BaseModel):
    """Detailed run metadata displayed for the selected dashboard run."""

    id: str
    trace_id: str
    root_span_id: str
    task_id: str
    status: str
    lifecycle_status: str
    task_result: str
    verifier_success: bool | None
    task_spec: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    resumed_from_checkpoint_id: int | None


class TrajectoryEventView(BaseModel):
    """One audit timeline event emitted by model, harness, runtime, knowledge, or skills."""

    id: int
    run_id: str
    step_index: int | None
    trace_id: str
    span_id: str
    event_type: str
    payload: dict[str, Any]
    task_id: str | None
    agent_id: str | None
    created_at: datetime | None


class ModelCallView(BaseModel):
    """One persisted model call with raw output, parsed action, and usage metadata."""

    id: int
    run_id: str
    step_index: int
    trace_id: str
    span_id: str
    raw_content: str
    action: dict[str, Any] | None
    usage: dict[str, Any]
    raw_response: dict[str, Any]
    source: str
    created_at: datetime | None


class RuntimeErrorView(BaseModel):
    """One runtime or worker error row tied to a run and optional step index."""

    id: int
    run_id: str
    step_index: int | None
    trace_id: str
    span_id: str
    error_type: str
    message: str
    payload: dict[str, Any]
    created_at: datetime | None


class ReplayStepView(BaseModel):
    """Step-centric evidence chain used to replay how one action was produced and executed."""

    step_index: int
    trace_id: str
    span_id: str
    parent_span_id: str
    span_status: str
    span_started_at: datetime | None
    span_finished_at: datetime | None
    status: str
    observation: dict[str, Any] | None
    context: dict[str, Any] | None
    resolved_terms: list[str]
    retrieved_docs: list[str]
    retrieved_skills: list[dict[str, Any]]
    retrieved_learning_candidates: list[dict[str, Any]]
    model_events: list[TrajectoryEventView]
    model_calls: list[ModelCallView]
    parsed_action: dict[str, Any] | None
    action_result: dict[str, Any] | None
    runtime_errors: list[RuntimeErrorView]
    highlights: list[str]
    raw_events: list[TrajectoryEventView]


class RunReplayView(BaseModel):
    """Replay payload that groups raw audit tables into a step-by-step evidence chain."""

    run: RunDetail
    run_events: list[TrajectoryEventView]
    steps: list[ReplayStepView]
    summary: dict[str, int]


class AgentAuditView(BaseModel):
    """Run-level agent audit snapshot assembled from persisted harness evidence."""

    run_id: str
    task_id: str
    run_status: str
    lifecycle_status: str
    task_result: str
    verifier_success: bool | None
    presence: str
    identity: dict[str, Any]
    current_task: dict[str, Any]
    latest_observation: dict[str, Any] | None
    latest_action: dict[str, Any] | None
    latest_action_result: dict[str, Any] | None
    latest_model_output: str | None
    latest_model_usage: dict[str, Any] | None
    token_totals: dict[str, float]
    reset: dict[str, Any] | None
    verifier: dict[str, Any] | None
    runtime_error_count: int
    latest_runtime_error: RuntimeErrorView | None
    event_counts: dict[str, int]
    latest_event_at: datetime | None


class AgentSummary(BaseModel):
    """Agent overview row derived from persisted run audit records."""

    key: str
    display_name: str
    username: str | None
    worker_id: str | None
    agent_id: str | None
    presence: str
    run_count: int
    active_run_count: int
    completed_run_count: int
    failed_run_count: int
    task_success_count: int
    task_failure_count: int
    skill_count: int
    promoted_skill_count: int
    latest_run_id: str | None
    latest_task_id: str | None
    latest_task_result: str
    latest_verifier_success: bool | None
    latest_event_at: datetime | None
    token_totals: dict[str, float]
    runtime_error_count: int


class AgentTaskSummary(BaseModel):
    """One task/run row shown inside an agent detail page."""

    run_id: str
    task_id: str
    status: str
    lifecycle_status: str
    task_result: str
    verifier_success: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    step_count: int
    event_count: int
    model_call_count: int
    runtime_error_count: int
    token_totals: dict[str, float]
    verifier: dict[str, Any] | None


class AgentDetailView(BaseModel):
    """Agent detail payload with overview, task history, and owned skills."""

    agent: AgentSummary
    runs: list[AgentTaskSummary]
    skills: list[SkillSummary]


class SkillSummary(BaseModel):
    """Compact skill row used by the dashboard review panel."""

    id: int
    name: str
    version: str
    status: str
    description: str
    triggers: list[str]
    action_count: int
    source_run_id: str | None
    updated_at: datetime | None


class SkillDetail(BaseModel):
    """Full skill review payload including the canonical SkillSpec JSON."""

    id: int
    name: str
    version: str
    status: str
    spec: dict[str, Any]
    source_run_id: str | None
    updated_at: datetime | None


class SkillDeprecateRequest(BaseModel):
    """Request body used when a reviewer deprecates a skill from the dashboard."""

    expected_updated_at: datetime
    reason: str = Field(default="dashboard review", max_length=500)


class SkillUpdateRequest(BaseModel):
    """Complete skill specification guarded by the row's last update timestamp."""

    spec: dict[str, Any]
    expected_updated_at: datetime


class SkillDeleteRequest(BaseModel):
    """Optimistic-lock metadata and optional audit reason for logical deletion."""

    expected_updated_at: datetime
    reason: str | None = Field(default=None, max_length=500)


class SkillLifecycleRequest(BaseModel):
    """Optimistic-lock token required by a skill lifecycle transition."""

    expected_updated_at: datetime


class SkillDeleteResult(BaseModel):
    """Identity of a skill that was removed from the active skill library."""

    id: int
    name: str
    version: str
    deleted: bool


class LearningCandidateView(BaseModel):
    """Auditable failure hypothesis and its support, recovery, and knowledge evidence."""

    id: int
    signature: str
    scope_key: str
    kind: str
    status: str
    hypothesis: str
    failure_status: str
    action_type: str
    target: str | None
    support_count: int
    recovery_count: int
    contradiction_count: int
    confidence: float
    evidence: dict[str, Any]
    knowledge_refs: list[dict[str, Any]]
    source_run_ids: list[str]
    recovery_run_ids: list[str]
    created_at: datetime | None
    updated_at: datetime | None


class BenchmarkModeView(BaseModel):
    """One row in the Week 8 raw-codegen vs harness comparison table."""

    mode: str
    label: str
    status: str
    task_count: int | None
    success_count: int | None
    success_rate: float | None
    invalid_action_rate: float | None
    runtime_crash_rate: float | None
    total_steps: int | None
    total_tokens: int | None
    estimated_cost: float | None
    source: str
    notes: list[str]
    raw_baseline_results: list[dict[str, object]]


class BenchmarkComparisonView(BaseModel):
    """Dashboard view of the Week 8 benchmark comparison report."""

    comparison_id: str
    generated_at: str
    modes: list[BenchmarkModeView]


class EvaluationReportSummaryView(BaseModel):
    """Aggregate verifier-backed metrics across every persisted run."""

    total_runs: int
    unique_tasks: int
    succeeded: int
    failed: int
    cancelled: int
    running: int
    unverified: int
    success_rate: float | None
    total_steps: int
    model_calls: int
    runtime_errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float | None
    duration_seconds: float
    avg_duration_sec: float | None
    avg_steps_per_run: float | None


class EvaluationReportGroupView(BaseModel):
    """Aggregate evaluation metrics for one category or skill-usage cohort."""

    run_count: int
    unique_tasks: int
    succeeded: int
    failed: int
    cancelled: int
    running: int
    unverified: int
    success_rate: float | None
    total_steps: int
    model_calls: int
    runtime_errors: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float | None
    duration_seconds: float


class EvaluationCategoryView(EvaluationReportGroupView):
    """One task-category row in the persisted evaluation report."""

    category: str


class EvaluationSkillUsageView(EvaluationReportGroupView):
    """One skill-injection cohort in the persisted evaluation report."""

    mode: Literal["skill_injected", "no_skill_injected"]


class EvaluationRecentRunView(BaseModel):
    """One recent persisted run with its evaluation evidence and measured cost."""

    run_id: str
    task_id: str
    category: str
    lifecycle_status: str
    task_result: str
    result_bucket: Literal["succeeded", "failed", "cancelled", "running", "unverified"]
    verifier_success: bool | None
    skill_usage: Literal["skill_injected", "no_skill_injected"]
    step_count: int
    model_call_count: int
    runtime_error_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float | None
    duration_seconds: float | None
    started_at: datetime | None
    finished_at: datetime | None


class EvaluationReportsView(BaseModel):
    """Database-backed evaluation report for the dashboard."""

    generated_at: datetime
    summary: EvaluationReportSummaryView
    by_category: list[EvaluationCategoryView]
    by_skill_usage: list[EvaluationSkillUsageView]
    recent_runs: list[EvaluationRecentRunView]


class CreativeEvaluationView(BaseModel):
    """MineCLIP creative score, calibration state, trend, and key-frame metadata."""

    id: int
    run_id: str
    task_id: str
    status: str
    prompt: str
    score: float | None
    score_threshold: float | None
    success: bool | None
    scorer: str
    variant: str | None
    calibration_status: str
    frame_count: int
    window_count: int
    result: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None


class HumanReviewMediaView(BaseModel):
    """Guarded final video and screenshot URLs for one human review."""

    video_url: str | None
    image_url: str | None
    video_available: bool
    image_available: bool


class HumanReviewView(BaseModel):
    """Human review queue entry with sanitized evidence and concurrency version."""

    id: int
    run_id: str
    task_id: str
    task_name: str
    status: str
    submission_summary: str
    reviewer_id: str | None
    decision: str | None
    reason_codes: list[str]
    notes: str
    submitted_at: datetime | None
    decided_at: datetime | None
    version: int
    media: HumanReviewMediaView
    mineclip: dict[str, Any] | None
    created_at: datetime | None
    updated_at: datetime | None


class HumanReviewDecisionRequest(BaseModel):
    """Optimistically locked authoritative decision submitted by one reviewer."""

    decision: Literal["approved", "rejected", "revision_requested", "inconclusive"]
    reviewer_id: str = Field(default="local-reviewer", min_length=1, max_length=255)
    notes: str = Field(default="", max_length=4000)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    expected_version: int = Field(ge=1)


def get_session() -> Iterator[Session]:
    """Yield one SQLAlchemy session for dashboard route handlers."""

    with SessionLocal() as session:
        yield session


@router.get("/human-reviews", response_model=list[HumanReviewView])
def list_human_reviews(
    status: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[HumanReviewView]:
    """Return the creative-task review queue with final media availability."""

    statement: Select[tuple[HumanReviewRecord]] = select(HumanReviewRecord).order_by(
        desc(HumanReviewRecord.submitted_at)
    )
    if status:
        statement = statement.where(HumanReviewRecord.status.in_(status))
    records = session.scalars(statement.limit(limit)).all()
    return [_human_review_view(record) for record in records]


@router.get("/human-reviews/{run_id}", response_model=HumanReviewView)
def get_human_review(
    run_id: str,
    session: Session = Depends(get_session),
) -> HumanReviewView:
    """Return one human review without exposing local artifact paths."""

    return _human_review_view(_require_human_review(session, run_id))


@router.post("/human-reviews/{run_id}/decision", response_model=HumanReviewView)
def decide_human_review(
    run_id: str,
    request: HumanReviewDecisionRequest,
    session: Session = Depends(get_session),
) -> HumanReviewView:
    """Persist one authoritative decision and guard against stale concurrent reviewers."""

    record = session.scalar(
        select(HumanReviewRecord)
        .where(HumanReviewRecord.run_id == run_id)
        .with_for_update()
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Human review not found: {run_id}")
    if record.version != request.expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_review_version",
                "expected_version": request.expected_version,
                "current_version": record.version,
            },
        )
    if record.status != "awaiting_review":
        raise HTTPException(
            status_code=409,
            detail={"code": "review_already_decided", "status": record.status},
        )

    decided_at = datetime.now(tz=UTC)
    record.status = request.decision
    record.decision = request.decision
    record.reviewer_id = request.reviewer_id.strip()
    record.notes = request.notes.strip()
    record.reason_codes = _bounded_reason_codes(request.reason_codes)
    record.decided_at = decided_at
    record.version += 1
    run = _require_run(session, run_id)
    run.status = _run_status_for_human_decision(request.decision)
    run.finished_at = run.finished_at or decided_at
    _record_human_review_event(session, run, record)
    session.commit()
    session.refresh(record)
    return _human_review_view(record)


@router.get("/human-reviews/{run_id}/video")
def get_human_review_video(
    run_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Stream the trusted final run video for a human reviewer."""

    record = _require_human_review(session, run_id)
    path = _human_review_media_path(record, "video")
    if path is None:
        raise HTTPException(status_code=404, detail="Human review video is unavailable.")
    return FileResponse(path, media_type=_video_media_type(path))


@router.get("/human-reviews/{run_id}/image")
def get_human_review_image(
    run_id: str,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve the terminal screenshot selected for a human reviewer."""

    record = _require_human_review(session, run_id)
    path = _human_review_media_path(record, "image")
    if path is None:
        raise HTTPException(status_code=404, detail="Human review image is unavailable.")
    return FileResponse(path)


@router.get("/creative-evaluations", response_model=list[CreativeEvaluationView])
def list_creative_evaluations(
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[CreativeEvaluationView]:
    """Return recent external creative-task evaluations for the audit dashboard."""

    records = session.scalars(
        select(CreativeEvaluationRecord)
        .order_by(desc(CreativeEvaluationRecord.updated_at))
        .limit(limit)
    ).all()
    return [_creative_evaluation_view(record) for record in records]


@router.get("/creative-evaluations/{run_id}", response_model=CreativeEvaluationView)
def get_creative_evaluation(
    run_id: str,
    session: Session = Depends(get_session),
) -> CreativeEvaluationView:
    """Return the latest MineCLIP evaluation summary for one run."""

    record = session.scalar(
        select(CreativeEvaluationRecord).where(CreativeEvaluationRecord.run_id == run_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Creative evaluation not found: {run_id}")
    return _creative_evaluation_view(record)


@router.get("/creative-evaluations/{run_id}/frames/{frame_index}")
def get_creative_key_frame(
    run_id: str,
    frame_index: int,
    session: Session = Depends(get_session),
) -> FileResponse:
    """Serve one audited key frame while restricting access to the artifact root."""

    record = session.scalar(
        select(CreativeEvaluationRecord).where(CreativeEvaluationRecord.run_id == run_id)
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"Creative evaluation not found: {run_id}")
    key_frames = record.result.get("key_frames") if isinstance(record.result, dict) else None
    if not isinstance(key_frames, list) or frame_index < 0 or frame_index >= len(key_frames):
        raise HTTPException(status_code=404, detail=f"Creative key frame not found: {frame_index}")
    frame = key_frames[frame_index]
    raw_path = frame.get("path") if isinstance(frame, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        raise HTTPException(status_code=404, detail="Creative key frame has no artifact path.")
    path = Path(raw_path).expanduser().resolve()
    artifact_root = Path(settings.artifact_root).expanduser().resolve()
    if not path.is_relative_to(artifact_root):
        raise HTTPException(status_code=403, detail="Creative key frame is outside ARTIFACT_ROOT.")
    if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(status_code=404, detail="Creative key frame artifact is unavailable.")
    return FileResponse(path)


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[RunSummary]:
    """Return recent runs with denormalized audit counts for the run list."""

    runs = session.scalars(
        select(RunRecord).order_by(desc(RunRecord.started_at)).limit(limit)
    ).all()
    run_ids = [run.id for run in runs]
    step_counts = _count_by_run(session, StepRecord, run_ids)
    event_counts = _count_by_run(session, TrajectoryEventRecord, run_ids)
    model_call_counts = _count_by_run(session, ModelCallRecord, run_ids)
    runtime_error_counts = _count_by_run(session, RuntimeErrorRecord, run_ids)
    verifier_by_run = _latest_verifier_by_run(session, run_ids)
    return [
        RunSummary(
            id=run.id,
            trace_id=run.trace_id,
            root_span_id=run.root_span_id,
            task_id=run.task_id,
            status=run.status,
            lifecycle_status=run.status,
            task_result=_task_result(run.status, verifier_by_run.get(run.id)),
            verifier_success=_verifier_success(verifier_by_run.get(run.id)),
            started_at=run.started_at,
            finished_at=run.finished_at,
            step_count=step_counts.get(run.id, 0),
            event_count=event_counts.get(run.id, 0),
            model_call_count=model_call_counts.get(run.id, 0),
            runtime_error_count=runtime_error_counts.get(run.id, 0),
        )
        for run in runs
    ]


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, session: Session = Depends(get_session)) -> RunDetail:
    """Return detailed metadata for one persisted run."""

    run = session.get(RunRecord, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    verifier = _latest_verifier_by_run(session, [run.id]).get(run.id)
    return RunDetail(
        id=run.id,
        trace_id=run.trace_id,
        root_span_id=run.root_span_id,
        task_id=run.task_id,
        status=run.status,
        lifecycle_status=run.status,
        task_result=_task_result(run.status, verifier),
        verifier_success=_verifier_success(verifier),
        task_spec=run.task_spec,
        started_at=run.started_at,
        finished_at=run.finished_at,
        resumed_from_checkpoint_id=run.resumed_from_checkpoint_id,
    )


@router.get("/runs/{run_id}/events", response_model=list[TrajectoryEventView])
def list_run_events(
    run_id: str,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[TrajectoryEventView]:
    """Return timeline events for a run, optionally incrementally after an event id."""

    _require_run(session, run_id)
    statement = (
        select(TrajectoryEventRecord)
        .where(TrajectoryEventRecord.run_id == run_id, TrajectoryEventRecord.id > after_id)
        .order_by(TrajectoryEventRecord.id)
        .limit(limit)
    )
    return [_event_view(event) for event in session.scalars(statement).all()]


@router.get("/runs/{run_id}/model-calls", response_model=list[ModelCallView])
def list_model_calls(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[ModelCallView]:
    """Return model calls for the selected run in step order."""

    _require_run(session, run_id)
    statement = (
        select(ModelCallRecord)
        .where(ModelCallRecord.run_id == run_id)
        .order_by(ModelCallRecord.step_index, ModelCallRecord.id)
        .limit(limit)
    )
    return [_model_call_view(call) for call in session.scalars(statement).all()]


@router.get("/runs/{run_id}/runtime-errors", response_model=list[RuntimeErrorView])
def list_runtime_errors(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[RuntimeErrorView]:
    """Return runtime errors for the selected run in chronological order."""

    _require_run(session, run_id)
    statement = (
        select(RuntimeErrorRecord)
        .where(RuntimeErrorRecord.run_id == run_id)
        .order_by(RuntimeErrorRecord.id)
        .limit(limit)
    )
    return [_runtime_error_view(error) for error in session.scalars(statement).all()]


@router.get("/runs/{run_id}/replay", response_model=RunReplayView)
def get_run_replay(run_id: str, session: Session = Depends(get_session)) -> RunReplayView:
    """Return a step-centric replay assembled from trajectory and specialized audit tables."""

    run = _require_run(session, run_id)
    events = session.scalars(
        select(TrajectoryEventRecord)
        .where(TrajectoryEventRecord.run_id == run_id)
        .order_by(TrajectoryEventRecord.id)
    ).all()
    steps = session.scalars(
        select(StepRecord).where(StepRecord.run_id == run_id).order_by(StepRecord.step_index)
    ).all()
    model_calls = session.scalars(
        select(ModelCallRecord)
        .where(ModelCallRecord.run_id == run_id)
        .order_by(ModelCallRecord.step_index, ModelCallRecord.id)
    ).all()
    runtime_errors = session.scalars(
        select(RuntimeErrorRecord)
        .where(RuntimeErrorRecord.run_id == run_id)
        .order_by(RuntimeErrorRecord.id)
    ).all()
    round_spans = session.scalars(
        select(RoundSpanRecord)
        .where(RoundSpanRecord.run_id == run_id)
        .order_by(RoundSpanRecord.step_index)
    ).all()
    run_detail = RunDetail(
        id=run.id,
        trace_id=run.trace_id,
        root_span_id=run.root_span_id,
        task_id=run.task_id,
        status=run.status,
        lifecycle_status=run.status,
        task_result=_task_result(run.status, _latest_event_payload(events, "verifier_result")),
        verifier_success=_verifier_success(_latest_event_payload(events, "verifier_result")),
        task_spec=run.task_spec,
        started_at=run.started_at,
        finished_at=run.finished_at,
        resumed_from_checkpoint_id=run.resumed_from_checkpoint_id,
    )
    replay_steps, run_events = _build_replay_steps(
        events,
        steps,
        model_calls,
        runtime_errors,
        round_spans,
        run=run,
    )
    return RunReplayView(
        run=run_detail,
        run_events=run_events,
        steps=replay_steps,
        summary={
            "run_event_count": len(run_events),
            "step_count": len(replay_steps),
            "model_call_count": len(model_calls),
            "runtime_error_count": len(runtime_errors),
        },
    )


@router.get("/runs/{run_id}/agent-audit", response_model=AgentAuditView)
def get_agent_audit(run_id: str, session: Session = Depends(get_session)) -> AgentAuditView:
    """Return a compact agent audit snapshot for the selected run."""

    run = _require_run(session, run_id)
    events = session.scalars(
        select(TrajectoryEventRecord)
        .where(TrajectoryEventRecord.run_id == run_id)
        .order_by(TrajectoryEventRecord.id)
    ).all()
    model_calls = session.scalars(
        select(ModelCallRecord)
        .where(ModelCallRecord.run_id == run_id)
        .order_by(ModelCallRecord.step_index, ModelCallRecord.id)
    ).all()
    runtime_errors = session.scalars(
        select(RuntimeErrorRecord)
        .where(RuntimeErrorRecord.run_id == run_id)
        .order_by(RuntimeErrorRecord.id)
    ).all()
    return _agent_audit_view(run, events, model_calls, runtime_errors)


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[AgentSummary]:
    """Return agent overview rows grouped from persisted run audit records."""

    runs = session.scalars(
        select(RunRecord).order_by(desc(RunRecord.started_at)).limit(limit)
    ).all()
    return _agent_summaries(session, runs)


@router.get("/agents/{agent_key}", response_model=AgentDetailView)
def get_agent_detail(agent_key: str, session: Session = Depends(get_session)) -> AgentDetailView:
    """Return one agent overview with its task history and skill inventory."""

    runs = session.scalars(select(RunRecord).order_by(desc(RunRecord.started_at))).all()
    agent_runs = [run for run in runs if _agent_key_for_run(run) == agent_key]
    if not agent_runs:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_key}")
    summaries = _agent_summaries(session, agent_runs)
    skills = _skills_for_run_ids(session, [run.id for run in agent_runs])
    return AgentDetailView(
        agent=summaries[0],
        runs=_agent_task_summaries(session, agent_runs),
        skills=[_skill_summary(skill) for skill in skills],
    )


@router.get("/runs/{run_id}/stream")
def stream_run_events(
    run_id: str,
    request: Request,
    after_id: int = Query(default=0, ge=0),
    poll_interval_sec: float = Query(default=0.75, ge=0.1, le=10.0),
    heartbeat_sec: float = Query(default=15.0, ge=1.0, le=60.0),
    batch_limit: int = Query(default=100, ge=1, le=1000),
    close_after_current_batch: bool = Query(default=False),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Stream trajectory events for one run using Server-Sent Events."""

    _require_run(session, run_id)
    stream_session_factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    return StreamingResponse(
        _run_event_stream(
            request=request,
            run_id=run_id,
            after_id=after_id,
            poll_interval_sec=poll_interval_sec,
            heartbeat_sec=heartbeat_sec,
            batch_limit=batch_limit,
            close_after_current_batch=close_after_current_batch,
            session_factory=stream_session_factory,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/learning-candidates", response_model=list[LearningCandidateView])
def list_learning_candidates(
    status: list[str] | None = Query(default=None),
    scope_key: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[LearningCandidateView]:
    """Return failure-derived hypotheses without presenting them as promoted skills."""

    statement: Select[tuple[LearningCandidateRecord]] = select(LearningCandidateRecord).order_by(
        desc(LearningCandidateRecord.updated_at)
    )
    if status:
        statement = statement.where(LearningCandidateRecord.status.in_(status))
    if scope_key:
        statement = statement.where(LearningCandidateRecord.scope_key == scope_key)
    records = session.scalars(statement.limit(limit)).all()
    return [_learning_candidate_view(record) for record in records]


@router.get("/learning-candidates/{candidate_id}", response_model=LearningCandidateView)
def get_learning_candidate(
    candidate_id: int,
    session: Session = Depends(get_session),
) -> LearningCandidateView:
    """Return full evidence for one failure-learning candidate."""

    return _learning_candidate_view(_require_learning_candidate(session, candidate_id))


@router.get("/skills", response_model=list[SkillSummary])
def list_skills(
    status: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[SkillSummary]:
    """Return skill review rows, optionally filtered by lifecycle status."""

    statement: Select[tuple[SkillRecord]] = select(SkillRecord).order_by(
        SkillRecord.name,
        desc(SkillRecord.updated_at),
    )
    statement = statement.where(SkillRecord.status != SKILL_DELETED_STATUS)
    if status:
        statement = statement.where(SkillRecord.status.in_(status))
    records = session.scalars(statement.limit(limit)).all()
    return [_skill_summary(record) for record in records]


@router.get("/skills/{skill_id}", response_model=SkillDetail)
def get_skill(skill_id: int, session: Session = Depends(get_session)) -> SkillDetail:
    """Return one skill and its canonical JSON specification."""

    return _skill_detail(_require_skill(session, skill_id))


@router.patch(
    "/skills/{skill_id}",
    response_model=SkillDetail,
    dependencies=[Depends(require_local_control)],
)
def update_skill(
    skill_id: int,
    request: SkillUpdateRequest,
    session: Session = Depends(get_session),
) -> SkillDetail:
    """Replace an editable skill spec while preserving its source and lifecycle controls."""

    record = _require_skill(session, skill_id, for_update=True)
    _require_current_skill_version(record, request.expected_updated_at)
    try:
        submitted = SkillSpec.model_validate(request.spec)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid skill specification: {exc}",
        ) from exc
    current = _validated_skill_spec_for_mutation(record)
    if submitted.source_run_id != current.source_run_id:
        raise HTTPException(
            status_code=422,
            detail="The portable spec source_run_id is immutable provenance and cannot be changed.",
        )
    if submitted.status.value != record.status:
        raise HTTPException(
            status_code=409,
            detail=(
                "Skill status cannot be edited through the specification; "
                "use the lifecycle controls instead."
            ),
        )
    duplicate_id = session.scalar(
        select(SkillRecord.id).where(
            SkillRecord.name == submitted.name,
            SkillRecord.version == submitted.version,
            SkillRecord.id != record.id,
        )
    )
    if duplicate_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A skill named {submitted.name!r} at version {submitted.version!r} already exists.",
        )

    source_run_id = record.source_run_id
    before = _skill_detail(record).model_dump(mode="json")
    edited_at = datetime.now(UTC)
    record.name = submitted.name
    record.version = submitted.version
    record.status = submitted.status.value
    record.spec = _skill_payload_for_dashboard_edit(
        request.spec,
        submitted,
        edited_at=edited_at,
        bootstrap_origin=_bootstrap_origin(record, before),
    )
    record.updated_at = edited_at
    after = _skill_detail(record).model_dump(mode="json")
    with session.no_autoflush:
        _record_skill_review_event(
            session,
            source_run_id,
            "skill_updated",
            {
                "skill_id": record.id,
                "before": before,
                "after": after,
                "authority": "dashboard_operator",
            },
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A skill named {submitted.name!r} at version {submitted.version!r} already exists.",
        ) from exc
    session.refresh(record)
    return _skill_detail(record)


@router.delete(
    "/skills/{skill_id}",
    response_model=SkillDeleteResult,
    dependencies=[Depends(require_local_control)],
)
def delete_skill(
    skill_id: int,
    request: SkillDeleteRequest,
    session: Session = Depends(get_session),
) -> SkillDeleteResult:
    """Permanently tombstone a skill while preserving its specification and provenance."""

    record = _require_skill(session, skill_id, for_update=True)
    _require_current_skill_version(record, request.expected_updated_at)
    result = SkillDeleteResult(
        id=record.id,
        name=record.name,
        version=record.version,
        deleted=True,
    )
    deleted_at = datetime.now(UTC)
    raw_spec = dict(record.spec) if isinstance(record.spec, dict) else {}
    raw_spec["_dashboard_override"] = True
    raw_spec["_dashboard_edited_at"] = deleted_at.isoformat()
    raw_spec["_dashboard_deleted"] = {
        "deleted_at": deleted_at.isoformat(),
        "reason": request.reason,
        "authority": "dashboard_operator",
    }
    origin = _bootstrap_origin(record)
    if origin is not None:
        raw_spec["_bootstrap_origin"] = origin
    record.status = SKILL_DELETED_STATUS
    record.spec = raw_spec
    record.updated_at = deleted_at
    _record_skill_review_event(
        session,
        record.source_run_id,
        "skill_deleted",
        {
            "skill": _skill_detail(record).model_dump(mode="json"),
            "reason": request.reason,
            "authority": "dashboard_operator",
        },
    )
    session.commit()
    return result


@router.post(
    "/skills/{skill_id}/promote",
    response_model=SkillDetail,
    dependencies=[Depends(require_local_control)],
)
def promote_skill(
    skill_id: int,
    request: SkillLifecycleRequest,
    session: Session = Depends(get_session),
) -> SkillDetail:
    """Promote a reviewed skill candidate without deleting its source trajectory."""

    record = _require_skill(session, skill_id, for_update=True)
    _require_current_skill_version(record, request.expected_updated_at)
    spec = _validated_skill_spec_for_mutation(record)
    if spec.status == SkillStatus.deprecated or record.status == SkillStatus.deprecated.value:
        raise HTTPException(status_code=409, detail="Deprecated skills cannot be promoted.")
    promoted = spec.model_copy(update={"status": SkillStatus.promoted})
    edited_at = datetime.now(UTC)
    record.status = SkillStatus.promoted.value
    record.spec = _skill_payload_for_dashboard_edit(
        record.spec,
        promoted,
        edited_at=edited_at,
        bootstrap_origin=_bootstrap_origin(record),
    )
    record.updated_at = edited_at
    _record_skill_review_event(
        session,
        record.source_run_id,
        "skill_promoted",
        {**promoted.model_dump(mode="json"), "authority": "dashboard_operator"},
    )
    session.commit()
    session.refresh(record)
    return _skill_detail(record)


@router.post(
    "/skills/{skill_id}/deprecate",
    response_model=SkillDetail,
    dependencies=[Depends(require_local_control)],
)
def deprecate_skill(
    skill_id: int,
    request: SkillDeprecateRequest,
    session: Session = Depends(get_session),
) -> SkillDetail:
    """Deprecate a skill while preserving its JSON spec and source run reference."""

    record = _require_skill(session, skill_id, for_update=True)
    _require_current_skill_version(record, request.expected_updated_at)
    spec = _validated_skill_spec_for_mutation(record)
    metrics = dict(spec.metrics)
    metrics["deprecation_reason"] = request.reason
    deprecated = spec.model_copy(update={"status": SkillStatus.deprecated, "metrics": metrics})
    edited_at = datetime.now(UTC)
    record.status = SkillStatus.deprecated.value
    record.spec = _skill_payload_for_dashboard_edit(
        record.spec,
        deprecated,
        edited_at=edited_at,
        bootstrap_origin=_bootstrap_origin(record),
    )
    record.updated_at = edited_at
    _record_skill_review_event(
        session,
        record.source_run_id,
        "skill_deprecated",
        {
            **deprecated.model_dump(mode="json"),
            "reason": request.reason,
            "authority": "dashboard_operator",
        },
    )
    session.commit()
    session.refresh(record)
    return _skill_detail(record)


@router.get("/evaluation-reports", response_model=EvaluationReportsView)
def get_evaluation_reports(
    recent_limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> EvaluationReportsView:
    """Aggregate real persisted runs without inferring prices or treating completion as success."""

    runs = list(session.scalars(select(RunRecord).order_by(desc(RunRecord.started_at))).all())
    run_ids = [run.id for run in runs]
    step_counts = _count_by_run(session, StepRecord, run_ids)
    runtime_error_counts = _count_by_run(session, RuntimeErrorRecord, run_ids)
    verifier_by_run = _latest_verifier_by_run(session, run_ids)

    usages_by_run: dict[str, list[dict[str, Any]]] = {}
    if run_ids:
        usage_rows = session.execute(
            select(ModelCallRecord.run_id, ModelCallRecord.usage)
            .where(ModelCallRecord.run_id.in_(run_ids))
            .order_by(ModelCallRecord.run_id, ModelCallRecord.id)
        ).all()
        for run_id, usage in usage_rows:
            usages_by_run.setdefault(str(run_id), []).append(
                usage if isinstance(usage, dict) else {}
            )

    skill_injected_by_run = dict.fromkeys(run_ids, False)
    if run_ids:
        context_rows = session.execute(
            select(TrajectoryEventRecord.run_id, TrajectoryEventRecord.payload).where(
                TrajectoryEventRecord.run_id.in_(run_ids),
                TrajectoryEventRecord.event_type == "context_built",
            )
        ).all()
        for run_id, payload in context_rows:
            if _context_injected_skill(payload):
                skill_injected_by_run[str(run_id)] = True

    rows: list[dict[str, Any]] = []
    for run in runs:
        verifier_payload = verifier_by_run.get(run.id)
        task_result = _task_result(run.status, verifier_payload)
        usage = _evaluation_model_usage(usages_by_run.get(run.id, []))
        rows.append(
            {
                "run_id": run.id,
                "task_id": run.task_id,
                "category": _evaluation_category(run),
                "lifecycle_status": run.status,
                "task_result": task_result,
                "result_bucket": _evaluation_result_bucket(run.status, task_result),
                "verifier_success": _verifier_success(verifier_payload),
                "skill_usage": (
                    "skill_injected"
                    if skill_injected_by_run.get(run.id, False)
                    else "no_skill_injected"
                ),
                "step_count": step_counts.get(run.id, 0),
                "model_call_count": len(usages_by_run.get(run.id, [])),
                "runtime_error_count": runtime_error_counts.get(run.id, 0),
                **usage,
                "duration_seconds": _run_duration_seconds(run),
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
        )

    summary_metrics = _evaluation_metrics(rows)
    measured_durations = [
        float(row["duration_seconds"])
        for row in rows
        if isinstance(row.get("duration_seconds"), int | float)
        and not isinstance(row.get("duration_seconds"), bool)
    ]
    summary = EvaluationReportSummaryView(
        total_runs=summary_metrics.pop("run_count"),
        avg_duration_sec=(
            round(sum(measured_durations) / len(measured_durations), 3)
            if measured_durations
            else None
        ),
        avg_steps_per_run=(
            round(sum(int(row["step_count"]) for row in rows) / len(rows), 3) if rows else None
        ),
        **summary_metrics,
    )
    categories = sorted({str(row["category"]) for row in rows})
    by_category = [
        EvaluationCategoryView(
            category=category,
            **_evaluation_metrics([row for row in rows if row["category"] == category]),
        )
        for category in categories
    ]
    by_skill_usage = [
        EvaluationSkillUsageView(
            mode=mode,
            **_evaluation_metrics([row for row in rows if row["skill_usage"] == mode]),
        )
        for mode in ("skill_injected", "no_skill_injected")
    ]
    recent_runs = [EvaluationRecentRunView.model_validate(row) for row in rows[:recent_limit]]
    return EvaluationReportsView(
        generated_at=datetime.now(UTC),
        summary=summary,
        by_category=by_category,
        by_skill_usage=by_skill_usage,
        recent_runs=recent_runs,
    )


@router.get("/benchmark-comparison", response_model=BenchmarkComparisonView)
def get_benchmark_comparison() -> BenchmarkComparisonView:
    """Return the current Week 8 comparison report assembled from local artifacts."""

    project_root = Path(__file__).resolve().parents[5]
    report = build_week8_comparison(
        week6_report_dir=project_root / "runs" / "week6",
        comparison_id="week8_dashboard_current",
    )
    return BenchmarkComparisonView.model_validate(report.to_dict())


def _count_by_run(session: Session, model: type[Any], run_ids: list[str]) -> dict[str, int]:
    """Count records grouped by run_id for a dashboard summary table."""

    if not run_ids:
        return {}
    rows = session.execute(
        select(model.run_id, func.count()).where(model.run_id.in_(run_ids)).group_by(model.run_id)
    ).all()
    return {str(run_id): int(count) for run_id, count in rows}


def _evaluation_model_usage(usages: list[dict[str, Any]]) -> dict[str, int | float | None]:
    """Sum recorded tokens and only costs explicitly persisted by the model provider."""

    input_tokens = sum(_usage_int(usage, "input_tokens") for usage in usages)
    output_tokens = sum(_usage_int(usage, "output_tokens") for usage in usages)
    total_tokens = sum(
        _usage_int(usage, "total_tokens")
        if _usage_has_number(usage, "total_tokens")
        else _usage_int(usage, "input_tokens") + _usage_int(usage, "output_tokens")
        for usage in usages
    )
    explicit_costs = [cost for usage in usages if (cost := _explicit_usage_cost(usage)) is not None]
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": (
            round(sum(explicit_costs), 10)
            if usages and len(explicit_costs) == len(usages)
            else None
        ),
    }


def _evaluation_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate normalized per-run report rows into one dashboard metric block."""

    succeeded = sum(row["result_bucket"] == "succeeded" for row in rows)
    failed = sum(row["result_bucket"] == "failed" for row in rows)
    evaluated = succeeded + failed
    costs = [
        float(row["estimated_cost"])
        for row in rows
        if isinstance(row.get("estimated_cost"), int | float)
        and not isinstance(row.get("estimated_cost"), bool)
    ]
    durations = [
        float(row["duration_seconds"])
        for row in rows
        if isinstance(row.get("duration_seconds"), int | float)
        and not isinstance(row.get("duration_seconds"), bool)
    ]
    model_calls = sum(int(row["model_call_count"]) for row in rows)
    has_unknown_cost = any(
        int(row["model_call_count"]) > 0 and row.get("estimated_cost") is None for row in rows
    )
    return {
        "run_count": len(rows),
        "unique_tasks": len({str(row["task_id"]) for row in rows}),
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": sum(row["result_bucket"] == "cancelled" for row in rows),
        "running": sum(row["result_bucket"] == "running" for row in rows),
        "unverified": sum(row["result_bucket"] == "unverified" for row in rows),
        "success_rate": round(succeeded / evaluated, 6) if evaluated else None,
        "total_steps": sum(int(row["step_count"]) for row in rows),
        "model_calls": model_calls,
        "runtime_errors": sum(int(row["runtime_error_count"]) for row in rows),
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "total_tokens": sum(int(row["total_tokens"]) for row in rows),
        "estimated_cost": (
            round(sum(costs), 10) if model_calls and not has_unknown_cost else None
        ),
        "duration_seconds": round(sum(durations), 3),
    }


def _context_injected_skill(payload: Any) -> bool:
    """Detect an actual rule-driven skill injection from a context_built audit event."""

    if not isinstance(payload, dict):
        return False
    injection = payload.get("skill_injection")
    if not isinstance(injection, dict):
        prompt_sections = payload.get("prompt_sections")
        injection = (
            prompt_sections.get("skill_injection") if isinstance(prompt_sections, dict) else None
        )
    return isinstance(injection, dict) and bool(injection.get("newly_injected"))


def _evaluation_category(run: RunRecord) -> str:
    """Read a normalized task category, with a bounded legacy task-id fallback."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    generic_category: str | None = None
    for key in ("category", "family"):
        raw_value = task_spec.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        category = raw_value.strip().lower().replace("-", "_").replace(" ", "_")
        category = {"tech_tree": "techtree"}.get(category, category)
        if category not in {"programmatic", "task", "unknown"}:
            return category
        generic_category = category

    normalized_task_id = run.task_id.lower()
    for separator in (":", "-", "/", "."):
        normalized_task_id = normalized_task_id.replace(separator, "_")
    tokens = set(normalized_task_id.split("_"))
    for category in ("creative", "harvest", "combat", "techtree", "survival"):
        if category in tokens:
            return category
    return generic_category or "unknown"


def _evaluation_result_bucket(
    lifecycle_status: str,
    task_result: str,
) -> Literal["succeeded", "failed", "cancelled", "running", "unverified"]:
    """Group lifecycle and verifier states without fabricating an evaluation outcome."""

    if task_result == "succeeded":
        return "succeeded"
    if task_result == "failed":
        return "failed"
    if lifecycle_status in {"cancelled", "interrupted", "terminated"}:
        return "cancelled"
    if lifecycle_status == "running":
        return "running"
    return "unverified"


def _run_duration_seconds(run: RunRecord) -> float | None:
    """Return measured wall-clock duration only when both persisted timestamps exist."""

    if run.started_at is None or run.finished_at is None:
        return None
    try:
        duration = (run.finished_at - run.started_at).total_seconds()
    except TypeError:
        duration = (
            run.finished_at.replace(tzinfo=None) - run.started_at.replace(tzinfo=None)
        ).total_seconds()
    return round(max(duration, 0.0), 3)


def _usage_has_number(usage: dict[str, Any], key: str) -> bool:
    """Return whether a usage field is an integer-compatible number."""

    value = usage.get(key)
    if isinstance(value, bool) or value is None:
        return False
    try:
        int(value)
    except (TypeError, ValueError):
        return False
    return True


def _usage_int(usage: dict[str, Any], key: str) -> int:
    """Read a non-negative integer usage field without trusting arbitrary JSON values."""

    if not _usage_has_number(usage, key):
        return 0
    return max(int(usage[key]), 0)


def _explicit_usage_cost(usage: dict[str, Any]) -> float | None:
    """Return only a provider-recorded cost or estimated_cost field."""

    for key in ("estimated_cost", "cost"):
        value = usage.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError):
            continue
        if cost >= 0:
            return cost
    return None


def _build_replay_steps(
    events: list[TrajectoryEventRecord],
    steps: list[StepRecord],
    model_calls: list[ModelCallRecord],
    runtime_errors: list[RuntimeErrorRecord],
    round_spans: list[RoundSpanRecord],
    *,
    run: RunRecord,
) -> tuple[list[ReplayStepView], list[TrajectoryEventView]]:
    """Group persisted audit records by step_index while preserving run-level events."""

    event_views_by_step: dict[int, list[TrajectoryEventView]] = {}
    run_events: list[TrajectoryEventView] = []
    for event in events:
        view = _event_view(event)
        step_index = event.step_index
        if step_index is None:
            step_index = _payload_step_index(event.payload)
        if step_index is None:
            run_events.append(view)
        else:
            event_views_by_step.setdefault(step_index, []).append(view)

    steps_by_index = {step.step_index: step for step in steps}
    calls_by_step: dict[int, list[ModelCallView]] = {}
    for call in model_calls:
        calls_by_step.setdefault(call.step_index, []).append(_model_call_view(call))

    errors_by_step: dict[int, list[RuntimeErrorView]] = {}
    for error in runtime_errors:
        view = _runtime_error_view(error)
        if error.step_index is None:
            continue
        errors_by_step.setdefault(error.step_index, []).append(view)

    spans_by_step = {span.step_index: span for span in round_spans}
    step_indexes = sorted(
        set(event_views_by_step)
        | set(steps_by_index)
        | set(calls_by_step)
        | set(errors_by_step)
        | set(spans_by_step)
    )
    return [
        _replay_step(
            step_index,
            run=run,
            span=spans_by_step.get(step_index),
            step=steps_by_index.get(step_index),
            events=event_views_by_step.get(step_index, []),
            model_calls=calls_by_step.get(step_index, []),
            runtime_errors=errors_by_step.get(step_index, []),
        )
        for step_index in step_indexes
    ], run_events


def _agent_summaries(session: Session, runs: list[RunRecord]) -> list[AgentSummary]:
    """Group run records into agent overview rows."""

    grouped_runs: dict[str, list[RunRecord]] = {}
    for run in runs:
        grouped_runs.setdefault(_agent_key_for_run(run), []).append(run)
    run_ids = [run.id for run in runs]
    model_calls_by_run = _model_calls_by_run(session, run_ids)
    runtime_error_counts = _count_by_run(session, RuntimeErrorRecord, run_ids)
    skills_by_run = _skills_by_run_id(session, run_ids)
    verifier_by_run = _latest_verifier_by_run(session, run_ids)
    summaries: list[AgentSummary] = []
    for agent_key, agent_runs in grouped_runs.items():
        sorted_runs = sorted(
            agent_runs, key=lambda run: run.started_at or datetime.min, reverse=True
        )
        latest_run = sorted_runs[0]
        identity = _agent_identity_for_run(latest_run)
        agent_run_ids = [run.id for run in sorted_runs]
        agent_skills = [
            skill for run_id in agent_run_ids for skill in skills_by_run.get(run_id, [])
        ]
        agent_verifier_successes = [
            _verifier_success(verifier_by_run.get(run_id)) for run_id in agent_run_ids
        ]
        summaries.append(
            AgentSummary(
                key=agent_key,
                display_name=_agent_display_name(identity, agent_key),
                username=_string_or_none(identity.get("username")),
                worker_id=_string_or_none(identity.get("worker_id")),
                agent_id=_string_or_none(identity.get("agent_id")),
                presence=_presence_for_agent(sorted_runs),
                run_count=len(sorted_runs),
                active_run_count=sum(1 for run in sorted_runs if run.status == "running"),
                completed_run_count=sum(
                    1 for run in sorted_runs if run.status in {"completed", "succeeded"}
                ),
                failed_run_count=sum(
                    1
                    for run in sorted_runs
                    if run.status
                    in {
                        "failed",
                        "task_timeout",
                        "model_timeout",
                        "runtime_error",
                        "verification_inconclusive",
                    }
                ),
                task_success_count=sum(
                    1 for success in agent_verifier_successes if success is True
                ),
                task_failure_count=sum(
                    1 for success in agent_verifier_successes if success is False
                ),
                skill_count=len(agent_skills),
                promoted_skill_count=sum(
                    1 for skill in agent_skills if skill.status == SkillStatus.promoted.value
                ),
                latest_run_id=latest_run.id,
                latest_task_id=latest_run.task_id,
                latest_task_result=_task_result(
                    latest_run.status,
                    verifier_by_run.get(latest_run.id),
                ),
                latest_verifier_success=_verifier_success(verifier_by_run.get(latest_run.id)),
                latest_event_at=latest_run.finished_at or latest_run.started_at,
                token_totals=_usage_totals(
                    [
                        call
                        for run_id in agent_run_ids
                        for call in model_calls_by_run.get(run_id, [])
                    ]
                ),
                runtime_error_count=sum(
                    runtime_error_counts.get(run_id, 0) for run_id in agent_run_ids
                ),
            )
        )
    return sorted(
        summaries, key=lambda summary: summary.latest_event_at or datetime.min, reverse=True
    )


def _agent_task_summaries(session: Session, runs: list[RunRecord]) -> list[AgentTaskSummary]:
    """Build task/run rows for an agent detail page."""

    run_ids = [run.id for run in runs]
    step_counts = _count_by_run(session, StepRecord, run_ids)
    event_counts = _count_by_run(session, TrajectoryEventRecord, run_ids)
    model_calls_by_run = _model_calls_by_run(session, run_ids)
    runtime_error_counts = _count_by_run(session, RuntimeErrorRecord, run_ids)
    verifier_by_run = _latest_verifier_by_run(session, run_ids)
    return [
        AgentTaskSummary(
            run_id=run.id,
            task_id=run.task_id,
            status=run.status,
            lifecycle_status=run.status,
            task_result=_task_result(run.status, verifier_by_run.get(run.id)),
            verifier_success=_verifier_success(verifier_by_run.get(run.id)),
            started_at=run.started_at,
            finished_at=run.finished_at,
            step_count=step_counts.get(run.id, 0),
            event_count=event_counts.get(run.id, 0),
            model_call_count=len(model_calls_by_run.get(run.id, [])),
            runtime_error_count=runtime_error_counts.get(run.id, 0),
            token_totals=_usage_totals(model_calls_by_run.get(run.id, [])),
            verifier=verifier_by_run.get(run.id),
        )
        for run in sorted(runs, key=lambda record: record.started_at or datetime.min, reverse=True)
    ]


def _agent_key_for_run(run: RunRecord) -> str:
    """Return a stable dashboard key for the agent that produced a run."""

    identity = _agent_identity_for_run(run)
    if identity.get("agent_id"):
        return f"agent:{identity['agent_id']}"
    if identity.get("username"):
        return f"username:{identity['username']}"
    if identity.get("worker_id"):
        return f"worker:{identity['worker_id']}"
    return f"run:{run.id}"


def _agent_identity_for_run(run: RunRecord) -> dict[str, Any]:
    """Extract persisted agent identity fields from a run task specification."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    runtime = _dict_field(task_spec, "runtime")
    training = _dict_field(task_spec, "training")
    return {
        "agent_id": task_spec.get("agent_id"),
        "username": runtime.get("username"),
        "worker_id": training.get("worker_id"),
        "host": runtime.get("host"),
        "port": runtime.get("port"),
        "job_id": training.get("job_id"),
        "mode": training.get("mode"),
        "memory_namespace": training.get("memory_namespace"),
    }


def _agent_display_name(identity: dict[str, Any], fallback: str) -> str:
    """Choose the most readable name for an agent summary row."""

    for field in ("username", "agent_id", "worker_id"):
        value = identity.get(field)
        if value:
            return str(value)
    return fallback


def _presence_for_agent(runs: list[RunRecord]) -> str:
    """Infer agent presence from the lifecycle state of its runs."""

    if any(run.status == "running" for run in runs):
        return "online"
    if runs:
        return "finished"
    return "unknown"


def _model_calls_by_run(session: Session, run_ids: list[str]) -> dict[str, list[ModelCallRecord]]:
    """Load model calls grouped by run id."""

    grouped: dict[str, list[ModelCallRecord]] = {}
    if not run_ids:
        return grouped
    records = session.scalars(
        select(ModelCallRecord)
        .where(ModelCallRecord.run_id.in_(run_ids))
        .order_by(ModelCallRecord.run_id, ModelCallRecord.step_index, ModelCallRecord.id)
    ).all()
    for record in records:
        grouped.setdefault(record.run_id, []).append(record)
    return grouped


def _skills_by_run_id(session: Session, run_ids: list[str]) -> dict[str, list[SkillRecord]]:
    """Load skills grouped by their source run id."""

    grouped: dict[str, list[SkillRecord]] = {}
    if not run_ids:
        return grouped
    records = _skills_for_run_ids(session, run_ids)
    for record in records:
        if record.source_run_id is not None:
            grouped.setdefault(record.source_run_id, []).append(record)
    return grouped


def _skills_for_run_ids(session: Session, run_ids: list[str]) -> list[SkillRecord]:
    """Load all skills whose source trajectory belongs to the given run ids."""

    if not run_ids:
        return []
    return list(
        session.scalars(
            select(SkillRecord)
            .where(
                SkillRecord.source_run_id.in_(run_ids),
                SkillRecord.status != SKILL_DELETED_STATUS,
            )
            .order_by(SkillRecord.name, desc(SkillRecord.updated_at))
        ).all()
    )


def _latest_verifier_by_run(session: Session, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return the latest verifier event payload for each run id."""

    verifier_by_run: dict[str, dict[str, Any]] = {}
    if not run_ids:
        return verifier_by_run
    records = session.scalars(
        select(TrajectoryEventRecord)
        .where(
            TrajectoryEventRecord.run_id.in_(run_ids),
            TrajectoryEventRecord.event_type == "verifier_result",
        )
        .order_by(TrajectoryEventRecord.run_id, TrajectoryEventRecord.id)
    ).all()
    for record in records:
        verifier_by_run[record.run_id] = record.payload
    return verifier_by_run


def _verifier_success(verifier_payload: dict[str, Any] | None) -> bool | None:
    """Extract a verifier success flag from a persisted verifier event payload."""

    if not isinstance(verifier_payload, dict):
        return None
    success = verifier_payload.get("success")
    if isinstance(success, bool):
        return success
    nested = verifier_payload.get("verifier")
    if isinstance(nested, dict) and isinstance(nested.get("success"), bool):
        return bool(nested["success"])
    return None


def _task_result(lifecycle_status: str, verifier_payload: dict[str, Any] | None) -> str:
    """Map verifier evidence and lifecycle state into a user-facing task result."""

    success = _verifier_success(verifier_payload)
    if success is True:
        return "succeeded"
    if success is False:
        return "failed"
    if lifecycle_status == "running":
        return "pending"
    if lifecycle_status == "awaiting_human_review":
        return "awaiting_review"
    if lifecycle_status == "revision_requested":
        return "revision_requested"
    if lifecycle_status == "succeeded":
        return "succeeded"
    if lifecycle_status in {"task_timeout", "model_timeout", "verification_inconclusive"}:
        return lifecycle_status
    if lifecycle_status == "completed_unverified":
        return "unverified"
    if lifecycle_status in {"failed", "cancelled", "interrupted", "terminated", "runtime_error"}:
        return "not_evaluated"
    return "unknown"


async def _run_event_stream(
    request: Request,
    run_id: str,
    after_id: int,
    poll_interval_sec: float,
    heartbeat_sec: float,
    batch_limit: int,
    close_after_current_batch: bool,
    session_factory: Callable[[], Session],
) -> AsyncIterator[str]:
    """Yield SSE frames for trajectory events created after the supplied event id."""

    cursor = after_id
    idle_since_heartbeat = 0.0
    while not await request.is_disconnected():
        with session_factory() as session:
            events = session.scalars(
                select(TrajectoryEventRecord)
                .where(TrajectoryEventRecord.run_id == run_id, TrajectoryEventRecord.id > cursor)
                .order_by(TrajectoryEventRecord.id)
                .limit(batch_limit)
            ).all()
        if events:
            idle_since_heartbeat = 0.0
            for event in events:
                cursor = event.id
                yield _sse_frame("trajectory", str(event.id), _event_view(event).model_dump_json())
        elif close_after_current_batch:
            return
        else:
            idle_since_heartbeat += poll_interval_sec
            if idle_since_heartbeat >= heartbeat_sec:
                idle_since_heartbeat = 0.0
                yield _sse_frame(
                    "heartbeat",
                    None,
                    json.dumps({"run_id": run_id, "after_id": cursor}),
                )
        if close_after_current_batch:
            return
        await asyncio.sleep(poll_interval_sec)


def _sse_frame(event_name: str, event_id: str | None, data: str) -> str:
    """Format one Server-Sent Events frame."""

    lines = [f"event: {event_name}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _agent_audit_view(
    run: RunRecord,
    events: list[TrajectoryEventRecord],
    model_calls: list[ModelCallRecord],
    runtime_errors: list[RuntimeErrorRecord],
) -> AgentAuditView:
    """Build the run-level agent audit view used by the dashboard inspector."""

    task_spec = run.task_spec if isinstance(run.task_spec, dict) else {}
    runtime = _dict_field(task_spec, "runtime")
    training = _dict_field(task_spec, "training")
    latest_call = model_calls[-1] if model_calls else None
    latest_runtime_error = _runtime_error_view(runtime_errors[-1]) if runtime_errors else None
    latest_observation = _latest_payload_field(events, "observation", "observation")
    latest_action_result = _latest_payload_field(events, "action_result", "result")
    latest_model_event_action = _latest_payload_field(events, "model_action", "action")
    latest_action = (
        latest_call.action
        if latest_call is not None and latest_call.action
        else latest_model_event_action
    )
    reset_payload = _latest_event_payload(events, "environment_reset")
    verifier_payload = _latest_event_payload(events, "verifier_result")
    latest_event_at = max(
        (event.created_at for event in events if event.created_at is not None), default=None
    )
    return AgentAuditView(
        run_id=run.id,
        task_id=run.task_id,
        run_status=run.status,
        lifecycle_status=run.status,
        task_result=_task_result(run.status, verifier_payload),
        verifier_success=_verifier_success(verifier_payload),
        presence=_presence_for_run(run.status),
        identity={
            "agent_id": _first_agent_id(events),
            "username": runtime.get("username"),
            "worker_id": training.get("worker_id"),
            "host": runtime.get("host"),
            "port": runtime.get("port"),
            "job_id": training.get("job_id"),
            "mode": training.get("mode"),
            "memory_namespace": training.get("memory_namespace"),
        },
        current_task={
            "task_id": run.task_id,
            "goal": task_spec.get("goal"),
            "description": task_spec.get("description"),
            "success_criteria": task_spec.get("success_criteria"),
            "allowed_actions": task_spec.get("allowed_actions"),
            "knowledge_tags": task_spec.get("knowledge_tags"),
        },
        latest_observation=latest_observation if isinstance(latest_observation, dict) else None,
        latest_action=latest_action if isinstance(latest_action, dict) else None,
        latest_action_result=latest_action_result
        if isinstance(latest_action_result, dict)
        else None,
        latest_model_output=latest_call.raw_content
        if latest_call is not None
        else _latest_raw_model_output(events),
        latest_model_usage=latest_call.usage
        if latest_call is not None
        else _latest_model_usage(events),
        token_totals=_usage_totals(model_calls),
        reset=reset_payload,
        verifier=verifier_payload,
        runtime_error_count=len(runtime_errors),
        latest_runtime_error=latest_runtime_error,
        event_counts=_event_counts(events),
        latest_event_at=latest_event_at,
    )


def _replay_step(
    step_index: int,
    run: RunRecord,
    span: RoundSpanRecord | None,
    step: StepRecord | None,
    events: list[TrajectoryEventView],
    model_calls: list[ModelCallView],
    runtime_errors: list[RuntimeErrorView],
) -> ReplayStepView:
    """Build one replay step from all audit evidence tied to the same step index."""

    observation_event = _first_event(events, "observation")
    context_event = _first_event(events, "context_built")
    action_result_event = _first_event(events, "action_result")
    model_events = [event for event in events if event.event_type in _MODEL_REPLAY_EVENT_TYPES]
    observation = (
        step.observation
        if step is not None and step.observation
        else _payload_field(observation_event, "observation")
    )
    context = context_event.payload if context_event is not None else None
    action_result = (
        step.action_result
        if step is not None and step.action_result
        else _payload_field(action_result_event, "result")
    )
    parsed_action = _parsed_action(step, model_calls, model_events, action_result_event)
    trace_id = _round_trace_id(run, span, step, events, model_calls, runtime_errors)
    span_id = _round_span_id(run, step_index, span, step, events, model_calls, runtime_errors)
    replay_status = _replay_status(action_result, runtime_errors, model_events)
    return ReplayStepView(
        step_index=step_index,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=(
            span.parent_span_id
            if span is not None
            else run.root_span_id or root_span_id_for_run(run.id)
        ),
        span_status=span.status if span is not None else replay_status,
        span_started_at=span.started_at if span is not None else _round_started_at(
            step, events, model_calls, runtime_errors
        ),
        span_finished_at=span.finished_at if span is not None else _round_finished_at(
            step, events, model_calls, runtime_errors
        ),
        status=replay_status,
        observation=observation if isinstance(observation, dict) else None,
        context=context,
        resolved_terms=_string_list(context, "resolved_terms"),
        retrieved_docs=_string_list(context, "retrieved_docs"),
        retrieved_skills=_dict_list(context, "retrieved_skills"),
        retrieved_learning_candidates=_dict_list(context, "retrieved_learning_candidates"),
        model_events=model_events,
        model_calls=model_calls,
        parsed_action=parsed_action,
        action_result=action_result if isinstance(action_result, dict) else None,
        runtime_errors=runtime_errors,
        highlights=_replay_highlights(
            context, parsed_action, action_result, runtime_errors, model_events
        ),
        raw_events=events,
    )


def _round_trace_id(
    run: RunRecord,
    span: RoundSpanRecord | None,
    step: StepRecord | None,
    events: list[TrajectoryEventView],
    model_calls: list[ModelCallView],
    runtime_errors: list[RuntimeErrorView],
) -> str:
    """Return canonical trace identity while tolerating pre-migration test fixtures."""

    candidates = [
        span.trace_id if span is not None else None,
        step.trace_id if step is not None else None,
        events[0].trace_id if events else None,
        model_calls[0].trace_id if model_calls else None,
        runtime_errors[0].trace_id if runtime_errors else None,
        run.trace_id,
    ]
    return next((value for value in candidates if value), trace_id_for_run(run.id))


def _round_span_id(
    run: RunRecord,
    step_index: int,
    span: RoundSpanRecord | None,
    step: StepRecord | None,
    events: list[TrajectoryEventView],
    model_calls: list[ModelCallView],
    runtime_errors: list[RuntimeErrorView],
) -> str:
    """Return canonical round span identity from any persisted evidence source."""

    candidates = [
        span.span_id if span is not None else None,
        step.span_id if step is not None else None,
        events[0].span_id if events else None,
        model_calls[0].span_id if model_calls else None,
        runtime_errors[0].span_id if runtime_errors else None,
    ]
    return next(
        (value for value in candidates if value),
        span_id_for_round(run.id, step_index),
    )


def _round_started_at(
    step: StepRecord | None,
    events: list[TrajectoryEventView],
    model_calls: list[ModelCallView],
    runtime_errors: list[RuntimeErrorView],
) -> datetime | None:
    """Derive a fallback start time for fixtures without a RoundSpan row."""

    values = [
        step.created_at if step is not None else None,
        *(event.created_at for event in events),
        *(call.created_at for call in model_calls),
        *(error.created_at for error in runtime_errors),
    ]
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _round_finished_at(
    step: StepRecord | None,
    events: list[TrajectoryEventView],
    model_calls: list[ModelCallView],
    runtime_errors: list[RuntimeErrorView],
) -> datetime | None:
    """Derive a fallback end time for fixtures without a RoundSpan row."""

    if step is not None:
        return step.updated_at
    values = [
        *(event.created_at for event in events),
        *(call.created_at for call in model_calls),
        *(error.created_at for error in runtime_errors),
    ]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _parsed_action(
    step: StepRecord | None,
    model_calls: list[ModelCallView],
    model_events: list[TrajectoryEventView],
    action_result_event: TrajectoryEventView | None,
) -> dict[str, Any] | None:
    """Return the best available parsed action for a replay step."""

    if step is not None and step.action:
        return step.action
    for call in model_calls:
        if call.action is not None:
            return call.action
    for event in model_events:
        action = event.payload.get("action")
        if isinstance(action, dict):
            return action
    action = _payload_field(action_result_event, "action")
    return action if isinstance(action, dict) else None


def _replay_status(
    action_result: Any,
    runtime_errors: list[RuntimeErrorView],
    model_events: list[TrajectoryEventView],
) -> str:
    """Classify one replay step for compact visual scanning."""

    if runtime_errors:
        return "error"
    if isinstance(action_result, dict):
        if action_result.get("ok") is True:
            return "ok"
        if action_result.get("ok") is False:
            return "failed"
        return "completed"
    if any(
        event.event_type in {"model_error", "invalid_action", "model_repair_exhausted"}
        for event in model_events
    ):
        return "blocked"
    if model_events:
        return "pending"
    return "observed"


def _replay_highlights(
    context: dict[str, Any] | None,
    parsed_action: dict[str, Any] | None,
    action_result: Any,
    runtime_errors: list[RuntimeErrorView],
    model_events: list[TrajectoryEventView],
) -> list[str]:
    """Generate short evidence labels for the replay UI."""

    highlights: list[str] = []
    if context is not None:
        highlights.append(
            f"context: {len(_string_list(context, 'retrieved_docs'))} docs, "
            f"{len(_dict_list(context, 'retrieved_skills'))} skills, "
            f"{len(_dict_list(context, 'retrieved_learning_candidates'))} learning hypotheses"
        )
    if parsed_action is not None:
        highlights.append(f"action: {parsed_action.get('type', 'unknown')}")
    if isinstance(action_result, dict):
        if action_result.get("ok") is True:
            highlights.append("result: ok")
        elif action_result.get("ok") is False:
            highlights.append(f"result: {action_result.get('error_code') or 'failed'}")
    if runtime_errors:
        highlights.append(f"runtime errors: {len(runtime_errors)}")
    repair_events = [
        event
        for event in model_events
        if "repair" in event.event_type or event.event_type == "invalid_action"
    ]
    if repair_events:
        highlights.append(f"repair events: {len(repair_events)}")
    return highlights


def _event_view(event: TrajectoryEventRecord) -> TrajectoryEventView:
    """Convert a trajectory event record into an API response object."""

    return TrajectoryEventView(
        id=event.id,
        run_id=event.run_id,
        step_index=event.step_index,
        trace_id=event.trace_id,
        span_id=event.span_id,
        event_type=event.event_type,
        payload=event.payload,
        task_id=event.task_id,
        agent_id=event.agent_id,
        created_at=event.created_at,
    )


def _model_call_view(call: ModelCallRecord) -> ModelCallView:
    """Convert a model call record into an API response object."""

    return ModelCallView(
        id=call.id,
        run_id=call.run_id,
        step_index=call.step_index,
        trace_id=call.trace_id,
        span_id=call.span_id,
        raw_content=call.raw_content,
        action=call.action,
        usage=call.usage,
        raw_response=call.raw_response,
        source=call.source,
        created_at=call.created_at,
    )


def _runtime_error_view(error: RuntimeErrorRecord) -> RuntimeErrorView:
    """Convert a runtime error record into an API response object."""

    return RuntimeErrorView(
        id=error.id,
        run_id=error.run_id,
        step_index=error.step_index,
        trace_id=error.trace_id,
        span_id=error.span_id,
        error_type=error.error_type,
        message=error.message,
        payload=error.payload,
        created_at=error.created_at,
    )


def _payload_step_index(payload: dict[str, Any]) -> int | None:
    """Extract a valid integer step index from an event payload."""

    value = payload.get("step_index")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _first_event(events: list[TrajectoryEventView], event_type: str) -> TrajectoryEventView | None:
    """Return the first event of a given type in a replay step."""

    return next((event for event in events if event.event_type == event_type), None)


def _payload_field(event: TrajectoryEventView | None, field: str) -> Any:
    """Return one field from an optional event payload."""

    if event is None:
        return None
    return event.payload.get(field)


def _string_list(payload: dict[str, Any] | None, field: str) -> list[str]:
    """Return a JSON payload field as a list of strings."""

    if payload is None:
        return []
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_list(payload: dict[str, Any] | None, field: str) -> list[dict[str, Any]]:
    """Return a JSON payload field as a list of dictionaries."""

    if payload is None:
        return []
    value = payload.get(field)
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _dict_field(payload: dict[str, Any], field: str) -> dict[str, Any]:
    """Return a dictionary field from a JSON payload."""

    value = payload.get(field)
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    """Convert a present JSON scalar into a string, preserving missing values."""

    if value is None:
        return None
    return str(value)


def _latest_event_payload(
    events: list[TrajectoryEventRecord], event_type: str
) -> dict[str, Any] | None:
    """Return the newest payload for a trajectory event type."""

    for event in reversed(events):
        if event.event_type == event_type:
            return event.payload
    return None


def _latest_payload_field(events: list[TrajectoryEventRecord], event_type: str, field: str) -> Any:
    """Return one field from the newest payload for a trajectory event type."""

    payload = _latest_event_payload(events, event_type)
    if payload is None:
        return None
    return payload.get(field)


def _latest_raw_model_output(events: list[TrajectoryEventRecord]) -> str | None:
    """Return the newest raw model output persisted in trajectory events."""

    value = _latest_payload_field(events, "model_action", "raw_content")
    return value if isinstance(value, str) else None


def _latest_model_usage(events: list[TrajectoryEventRecord]) -> dict[str, Any] | None:
    """Return the newest model usage payload persisted in trajectory events."""

    value = _latest_payload_field(events, "model_action", "usage")
    return value if isinstance(value, dict) else None


def _usage_totals(model_calls: list[ModelCallRecord]) -> dict[str, float]:
    """Sum numeric usage fields across all model calls for a run."""

    totals: dict[str, float] = {}
    for call in model_calls:
        for key, value in call.usage.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            totals[key] = totals.get(key, 0.0) + float(value)
    return totals


def _event_counts(events: list[TrajectoryEventRecord]) -> dict[str, int]:
    """Count trajectory events by type for compact audit summaries."""

    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
    return counts


def _first_agent_id(events: list[TrajectoryEventRecord]) -> str | None:
    """Return the first persisted agent id when one exists."""

    for event in events:
        if event.agent_id:
            return event.agent_id
    return None


def _presence_for_run(status: str) -> str:
    """Map a persisted run lifecycle status to an audit presence label."""

    if status == "running":
        return "online"
    if status in {
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "task_timeout",
        "model_timeout",
        "runtime_error",
        "verification_inconclusive",
        "awaiting_human_review",
        "revision_requested",
        "terminated",
    }:
        return "finished"
    return "unknown"


def _require_run(session: Session, run_id: str) -> RunRecord:
    """Load a run or raise a 404 response."""

    run = session.get(RunRecord, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run


def _require_human_review(session: Session, run_id: str) -> HumanReviewRecord:
    """Load a human review by run id or raise a 404 response."""

    review = session.scalar(
        select(HumanReviewRecord).where(HumanReviewRecord.run_id == run_id)
    )
    if review is None:
        raise HTTPException(status_code=404, detail=f"Human review not found: {run_id}")
    return review


def _require_skill(
    session: Session,
    skill_id: int,
    *,
    for_update: bool = False,
) -> SkillRecord:
    """Load a skill or raise a 404 response."""

    statement = select(SkillRecord).where(
        SkillRecord.id == skill_id,
        SkillRecord.status != SKILL_DELETED_STATUS,
    )
    if for_update:
        statement = statement.with_for_update()
    skill = session.scalar(statement)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
    return skill


def _require_current_skill_version(
    record: SkillRecord,
    expected_updated_at: datetime,
) -> None:
    """Reject stale dashboard writes while tolerating SQLite's naive timestamps."""

    actual = _as_utc(record.updated_at)
    expected = _as_utc(expected_updated_at)
    if actual != expected:
        raise HTTPException(
            status_code=409,
            detail="Skill changed after it was loaded. Reload it before saving.",
        )


def _as_utc(value: datetime) -> datetime:
    """Normalize aware and SQLite-naive timestamps for optimistic-lock comparison."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_learning_candidate(
    session: Session,
    candidate_id: int,
) -> LearningCandidateRecord:
    """Load a learning candidate or raise a 404 response."""

    candidate = session.get(LearningCandidateRecord, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"Learning candidate not found: {candidate_id}")
    return candidate


def _learning_candidate_view(record: LearningCandidateRecord) -> LearningCandidateView:
    """Convert one candidate row into its complete dashboard audit representation."""

    return LearningCandidateView(
        id=record.id,
        signature=record.signature,
        scope_key=record.scope_key,
        kind=record.kind,
        status=record.status,
        hypothesis=record.hypothesis,
        failure_status=record.failure_status,
        action_type=record.action_type,
        target=record.target,
        support_count=record.support_count,
        recovery_count=record.recovery_count,
        contradiction_count=record.contradiction_count,
        confidence=record.confidence,
        evidence=dict(record.evidence or {}),
        knowledge_refs=list(record.knowledge_refs or []),
        source_run_ids=list(record.source_run_ids or []),
        recovery_run_ids=list(record.recovery_run_ids or []),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _creative_evaluation_view(record: CreativeEvaluationRecord) -> CreativeEvaluationView:
    """Convert one creative result while replacing private paths with frame API URLs."""

    result = dict(record.result or {})
    evidence_source = result.get("evidence_source")
    if isinstance(evidence_source, dict):
        public_source = {key: value for key, value in evidence_source.items() if key != "path"}
        raw_path = evidence_source.get("path")
        public_source["filename"] = Path(raw_path).name if isinstance(raw_path, str) else None
        result["evidence_source"] = public_source
    final_frame = result.get("final_frame")
    if isinstance(final_frame, dict):
        public_final_frame = {key: value for key, value in final_frame.items() if key != "path"}
        raw_path = final_frame.get("path")
        public_final_frame["filename"] = Path(raw_path).name if isinstance(raw_path, str) else None
        public_final_frame["image_url"] = f"/api/human-reviews/{record.run_id}/image"
        result["final_frame"] = public_final_frame
    key_frames = result.get("key_frames")
    if isinstance(key_frames, list):
        public_frames: list[dict[str, Any]] = []
        for index, frame in enumerate(key_frames):
            if not isinstance(frame, dict):
                continue
            public_frame = {key: value for key, value in frame.items() if key != "path"}
            raw_path = frame.get("path")
            public_frame["filename"] = Path(raw_path).name if isinstance(raw_path, str) else None
            public_frame["image_url"] = f"/api/creative-evaluations/{record.run_id}/frames/{index}"
            public_frames.append(public_frame)
        result["key_frames"] = public_frames
    return CreativeEvaluationView(
        id=record.id,
        run_id=record.run_id,
        task_id=record.task_id,
        status=record.status,
        prompt=record.prompt,
        score=record.score,
        score_threshold=record.score_threshold,
        success=record.success,
        scorer=record.scorer,
        variant=record.variant,
        calibration_status=record.calibration_status,
        frame_count=record.frame_count,
        window_count=record.window_count,
        result=result,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _human_review_view(record: HumanReviewRecord) -> HumanReviewView:
    """Convert a private review record into a path-free dashboard payload."""

    video = _human_review_media_path(record, "video")
    image = _human_review_media_path(record, "image")
    evidence = dict(record.evidence or {})
    mineclip = evidence.get("mineclip")
    return HumanReviewView(
        id=record.id,
        run_id=record.run_id,
        task_id=record.task_id,
        task_name=record.task_name,
        status=record.status,
        submission_summary=record.submission_summary,
        reviewer_id=record.reviewer_id,
        decision=record.decision,
        reason_codes=list(record.reason_codes or []),
        notes=record.notes,
        submitted_at=record.submitted_at,
        decided_at=record.decided_at,
        version=record.version,
        media=HumanReviewMediaView(
            video_url=f"/api/human-reviews/{record.run_id}/video" if video is not None else None,
            image_url=f"/api/human-reviews/{record.run_id}/image" if image is not None else None,
            video_available=video is not None,
            image_available=image is not None,
        ),
        mineclip=dict(mineclip) if isinstance(mineclip, dict) else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _human_review_media_path(
    record: HumanReviewRecord,
    media_kind: Literal["video", "image"],
) -> Path | None:
    """Resolve one trusted evidence artifact while preventing arbitrary file reads."""

    evidence = record.evidence if isinstance(record.evidence, dict) else {}
    raw_path: str | None = None
    allowed_suffixes: set[str]
    if media_kind == "video":
        source = evidence.get("source")
        if isinstance(source, dict) and source.get("type") == "video":
            raw_path = source.get("path") if isinstance(source.get("path"), str) else None
        allowed_suffixes = {".mp4", ".webm", ".mov", ".m4v"}
    else:
        final_frame = evidence.get("final_frame")
        if isinstance(final_frame, dict) and isinstance(final_frame.get("path"), str):
            raw_path = final_frame["path"]
        if raw_path is None:
            key_frames = evidence.get("key_frames")
            if isinstance(key_frames, list):
                for frame in reversed(key_frames):
                    if isinstance(frame, dict) and isinstance(frame.get("path"), str):
                        raw_path = frame["path"]
                        break
        allowed_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    if not raw_path:
        return None
    path = Path(raw_path).expanduser().resolve()
    artifact_root = Path(settings.artifact_root).expanduser().resolve()
    if not path.is_relative_to(artifact_root):
        return None
    if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
        return None
    return path


def _video_media_type(path: Path) -> str:
    """Return a stable media type for supported human-review video artifacts."""

    return {
        ".webm": "video/webm",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
    }.get(path.suffix.lower(), "video/mp4")


def _bounded_reason_codes(values: list[str]) -> list[str]:
    """Normalize reviewer reason codes into short unique audit labels."""

    normalized: list[str] = []
    for value in values:
        code = str(value).strip().lower().replace(" ", "_")[:64]
        if code and code not in normalized:
            normalized.append(code)
    return normalized[:20]


def _run_status_for_human_decision(decision: str) -> str:
    """Map one human decision onto the existing run lifecycle summary field."""

    return {
        "approved": "succeeded",
        "rejected": "failed",
        "revision_requested": "revision_requested",
        "inconclusive": "verification_inconclusive",
    }[decision]


def _record_human_review_event(
    session: Session,
    run: RunRecord,
    review: HumanReviewRecord,
) -> None:
    """Append the authoritative review decision to the immutable trajectory timeline."""

    run_identity = identity_from_task_spec(run.task_spec)
    event_payload, identity = enrich_event_payload(
        {
            "task_id": review.task_id,
            "review_id": review.id,
            "review_status": review.status,
            "decision": review.decision,
            "reviewer_id": review.reviewer_id,
            "reason_codes": list(review.reason_codes or []),
            "notes": review.notes,
            "review_version": review.version,
            "decided_at": review.decided_at.isoformat() if review.decided_at else None,
            "authority": "human",
        },
        AuditIdentity(
            task_id=run.task_id,
            agent_id=run_identity.agent_id,
            worker_id=run_identity.worker_id,
        ),
    )
    session.add(
        TrajectoryEventRecord(
            run_id=run.id,
            event_type="human_review_decided",
            payload=event_payload,
            task_id=identity.task_id,
            agent_id=identity.agent_id,
        )
    )


def _skill_summary(record: SkillRecord) -> SkillSummary:
    """Convert a SQL skill record into the compact dashboard row."""

    try:
        spec = SkillSpec.model_validate(record.spec)
    except ValidationError:
        raw_spec = record.spec if isinstance(record.spec, dict) else {}
        raw_action_plan = raw_spec.get("action_plan")
        action_plan = raw_action_plan if isinstance(raw_action_plan, list) else []
        return SkillSummary(
            id=record.id,
            name=record.name,
            version=record.version,
            status=record.status,
            description=str(
                raw_spec.get("description")
                or raw_spec.get("goal")
                or "Legacy skill specification"
            ),
            triggers=_string_list(raw_spec, "triggers"),
            action_count=len(action_plan),
            source_run_id=record.source_run_id,
            updated_at=record.updated_at,
        )
    return SkillSummary(
        id=record.id,
        name=record.name,
        version=record.version,
        status=record.status,
        description=spec.description,
        triggers=list(spec.triggers),
        action_count=len(spec.action_plan),
        source_run_id=record.source_run_id,
        updated_at=record.updated_at,
    )


def _validated_skill_spec_for_mutation(record: SkillRecord) -> SkillSpec:
    """Require the current schema before changing a historical skill lifecycle."""

    try:
        return SkillSpec.model_validate(record.spec)
    except ValidationError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "This historical skill uses a legacy specification. "
                "Review or migrate its action plan before changing its lifecycle."
            ),
        ) from exc


def _skill_payload_for_dashboard_edit(
    raw_spec: Any,
    normalized: SkillSpec,
    *,
    edited_at: datetime,
    bootstrap_origin: dict[str, str] | None,
) -> dict[str, Any]:
    """Persist normalized known fields without discarding portable extension metadata."""

    raw_payload = raw_spec if isinstance(raw_spec, dict) else {}
    extensions = {
        key: value for key, value in raw_payload.items() if key not in SkillSpec.model_fields
    }
    payload = {
        **extensions,
        **normalized.model_dump(mode="json"),
        "_dashboard_override": True,
        "_dashboard_edited_at": edited_at.isoformat(),
    }
    if bootstrap_origin is not None:
        payload["_bootstrap_origin"] = bootstrap_origin
    return payload


def _bootstrap_origin(
    record: SkillRecord,
    before: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """Retain the immutable seed identity when a bootstrap skill is renamed or tombstoned."""

    raw_spec = record.spec if isinstance(record.spec, dict) else {}
    existing = raw_spec.get("_bootstrap_origin")
    if (
        isinstance(existing, dict)
        and isinstance(existing.get("name"), str)
        and isinstance(existing.get("version"), str)
    ):
        return {"name": existing["name"], "version": existing["version"]}
    source_evidence = raw_spec.get("source_evidence")
    verifier_stats = raw_spec.get("verifier_stats")
    metrics = raw_spec.get("metrics")
    is_bootstrap = (
        isinstance(source_evidence, dict) and source_evidence.get("source") == "bootstrap"
    ) or (isinstance(verifier_stats, dict) and verifier_stats.get("bootstrap") is True) or (
        isinstance(metrics, dict) and metrics.get("bootstrap") is True
    )
    if not is_bootstrap:
        return None
    name = before.get("name") if isinstance(before, dict) else record.name
    version = before.get("version") if isinstance(before, dict) else record.version
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    return {"name": name, "version": version}


def _skill_detail(record: SkillRecord) -> SkillDetail:
    """Convert a SQL skill record into a full dashboard review payload."""

    return SkillDetail(
        id=record.id,
        name=record.name,
        version=record.version,
        status=record.status,
        spec=record.spec,
        source_run_id=record.source_run_id,
        updated_at=record.updated_at,
    )


def _record_skill_review_event(
    session: Session,
    run_id: str | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    """Attach a skill review event to the source run when that run is available."""

    if run_id is None:
        return
    run = session.get(RunRecord, run_id)
    if run is None:
        return
    run_identity = identity_from_task_spec(run.task_spec)
    event_payload, identity = enrich_event_payload(
        payload,
        AuditIdentity(
            task_id=run.task_id,
            agent_id=run_identity.agent_id,
            worker_id=run_identity.worker_id,
        ),
    )
    session.add(
        TrajectoryEventRecord(
            run_id=run_id,
            event_type=event_type,
            payload=event_payload,
            task_id=identity.task_id,
            agent_id=identity.agent_id,
        )
    )
