from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from mc_agent_harness.observability.tracing import (
    enrich_trace_payload,
    root_span_id_for_run,
    span_id_for_round,
    step_index_from_payload,
    trace_id_for_run,
)


SKILL_DELETED_STATUS = "deleted"


class Base(DeclarativeBase):
    """Declarative base for all harness persistence models."""


class TimestampMixin:
    """Reusable created/updated timestamp columns for audit records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RunRecord(TimestampMixin, Base):
    """One agent run with task metadata and lifecycle status."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    root_span_id: Mapped[str] = mapped_column(
        String(16),
        unique=True,
        index=True,
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False, default="running")
    task_spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resumed_from_checkpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("checkpoints.id"),
        nullable=True,
    )

    steps: Mapped[list[StepRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="StepRecord.run_id",
    )
    trajectory_events: Mapped[list[TrajectoryEventRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="TrajectoryEventRecord.run_id",
    )
    model_calls: Mapped[list[ModelCallRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="ModelCallRecord.run_id",
    )
    runtime_errors: Mapped[list[RuntimeErrorRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="RuntimeErrorRecord.run_id",
    )
    round_spans: Mapped[list[RoundSpanRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="RoundSpanRecord.run_id",
    )
    checkpoints: Mapped[list[CheckpointRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        foreign_keys="CheckpointRecord.run_id",
    )


class RoundSpanRecord(TimestampMixin, Base):
    """One durable trace span covering a complete agent execution round."""

    __tablename__ = "round_spans"
    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_round_spans_run_step"),
        UniqueConstraint("trace_id", "span_id", name="uq_round_spans_trace_span"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    parent_span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[RunRecord] = relationship(
        back_populates="round_spans",
        foreign_keys=[run_id],
    )


class StepRecord(TimestampMixin, Base):
    """One persisted observe-action-result step in a run."""

    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("run_id", "step_index", name="uq_steps_run_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    observation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    action: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    action_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[RunRecord] = relationship(
        back_populates="steps",
        foreign_keys=[run_id],
    )


class TrajectoryEventRecord(TimestampMixin, Base):
    """Raw typed event emitted by model, runtime, knowledge, memory, and skills."""

    __tablename__ = "trajectory_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    step_index: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)

    run: Mapped[RunRecord] = relationship(
        back_populates="trajectory_events",
        foreign_keys=[run_id],
    )


class ModelCallRecord(TimestampMixin, Base):
    """Model output and usage metadata for one action-generation attempt."""

    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="model")

    run: Mapped[RunRecord] = relationship(
        back_populates="model_calls",
        foreign_keys=[run_id],
    )


class RuntimeErrorRecord(TimestampMixin, Base):
    """Runtime or worker error captured while executing a run."""

    __tablename__ = "runtime_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[RunRecord] = relationship(
        back_populates="runtime_errors",
        foreign_keys=[run_id],
    )


class CreativeEvaluationRecord(TimestampMixin, Base):
    """Query-friendly MineCLIP evaluation summary for one creative-task run."""

    __tablename__ = "creative_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id"),
        unique=True,
        index=True,
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    scorer: Mapped[str] = mapped_column(String(128), nullable=False, default="mineclip")
    variant: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calibration_status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HumanReviewRecord(TimestampMixin, Base):
    """Authoritative human decision and evidence bundle for one creative-task run."""

    __tablename__ = "human_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id"),
        unique=True,
        index=True,
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    task_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
        default="awaiting_review",
    )
    submission_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    reviewer_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    decision: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TaskMemoryRecord(TimestampMixin, Base):
    """Task-scoped memory note persisted independently of other task namespaces."""

    __tablename__ = "task_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), index=True, nullable=False, default="default")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SkillRecord(TimestampMixin, Base):
    """Persisted skill specification and lifecycle status."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_skills_name_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)


class LearningCandidateRecord(TimestampMixin, Base):
    """Evidence-backed failure hypothesis awaiting successful recovery validation."""

    __tablename__ = "learning_candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(String(512), unique=True, index=True, nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    failure_status: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    support_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contradiction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    knowledge_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_run_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    recovery_run_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class KnowledgeChunkRecord(TimestampMixin, Base):
    """Local knowledge chunk indexed for deterministic retrieval and later pgvector search."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    chunk_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PromptConfigurationRecord(TimestampMixin, Base):
    """One persisted system-prompt or action-guide override."""

    __tablename__ = "prompt_configurations"
    __table_args__ = (
        UniqueConstraint(
            "kind",
            "config_key",
            name="uq_prompt_configurations_kind_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    config_key: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CheckpointRecord(TimestampMixin, Base):
    """Recoverable checkpoint state for a run."""

    __tablename__ = "checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True, nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    run: Mapped[RunRecord] = relationship(
        back_populates="checkpoints",
        foreign_keys=[run_id],
    )


@event.listens_for(RunRecord, "before_insert")
def _populate_run_trace_id(_mapper: Any, _connection: Any, target: RunRecord) -> None:
    """Guarantee a trace id for direct ORM inserts outside the main recorder."""

    target.trace_id = trace_id_for_run(target.id)
    target.root_span_id = root_span_id_for_run(target.id)


@event.listens_for(RoundSpanRecord, "before_insert")
def _populate_round_span_ids(
    _mapper: Any,
    _connection: Any,
    target: RoundSpanRecord,
) -> None:
    """Guarantee canonical trace context for one round span."""

    target.trace_id = trace_id_for_run(target.run_id)
    target.span_id = span_id_for_round(target.run_id, target.step_index)
    target.parent_span_id = root_span_id_for_run(target.run_id)


@event.listens_for(StepRecord, "before_insert")
def _populate_step_trace_ids(_mapper: Any, _connection: Any, target: StepRecord) -> None:
    """Guarantee canonical trace context for a materialized action-result step."""

    target.trace_id = trace_id_for_run(target.run_id)
    target.span_id = span_id_for_round(target.run_id, target.step_index)


@event.listens_for(TrajectoryEventRecord, "before_insert")
def _populate_event_trace_ids(
    _mapper: Any,
    _connection: Any,
    target: TrajectoryEventRecord,
) -> None:
    """Synchronize event columns and the portable JSON payload."""

    payload = dict(target.payload or {})
    step_index = target.step_index
    if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
        step_index = step_index_from_payload(payload)
    elif step_index_from_payload(payload) != step_index:
        payload["step_index"] = step_index
    target.payload = enrich_trace_payload(target.run_id, payload)
    target.trace_id = trace_id_for_run(target.run_id)
    target.step_index = step_index
    target.span_id = str(target.payload["span_id"])


@event.listens_for(ModelCallRecord, "before_insert")
def _populate_model_call_trace_ids(
    _mapper: Any,
    _connection: Any,
    target: ModelCallRecord,
) -> None:
    """Guarantee trace context for direct model-call persistence."""

    target.trace_id = trace_id_for_run(target.run_id)
    target.span_id = span_id_for_round(target.run_id, target.step_index)


@event.listens_for(RuntimeErrorRecord, "before_insert")
def _populate_runtime_error_trace_ids(
    _mapper: Any,
    _connection: Any,
    target: RuntimeErrorRecord,
) -> None:
    """Synchronize runtime-error columns and its JSON payload."""

    payload = dict(target.payload or {})
    step_index = target.step_index
    if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
        step_index = step_index_from_payload(payload)
    elif step_index_from_payload(payload) != step_index:
        payload["step_index"] = step_index
    target.step_index = step_index
    target.payload = enrich_trace_payload(target.run_id, payload)
    target.trace_id = trace_id_for_run(target.run_id)
    target.span_id = str(target.payload["span_id"])
