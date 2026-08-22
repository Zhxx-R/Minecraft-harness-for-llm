from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from mc_agent_harness.db.models import (
    CreativeEvaluationRecord,
    HumanReviewRecord,
    ModelCallRecord,
    RoundSpanRecord,
    RunRecord,
    RuntimeErrorRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.db.session import SessionFactory
from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.observability.identity import AuditIdentity, enrich_event_payload
from mc_agent_harness.observability.tracing import (
    enrich_trace_payload,
    root_span_id_for_run,
    span_id_for_round,
    step_index_from_payload,
    trace_id_for_run,
)


PersistedEventCallback = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class PersistentEvaluationRecorder(EvaluationRecorder):
    """Evaluation recorder that mirrors trajectory events into SQL tables."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        worker_id: str | None = None,
        event_callback: PersistedEventCallback | None = None,
    ) -> None:
        """Create a recorder with optional run-scoped audit identity defaults."""

        super().__init__()
        self.session_factory = session_factory
        self._observations: dict[tuple[str, int], dict[str, Any]] = {}
        self._default_identity = AuditIdentity(
            task_id=task_id,
            agent_id=agent_id,
            worker_id=worker_id,
        )
        self._run_identities: dict[str, AuditIdentity] = {}
        self._event_callback = event_callback

    async def record(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Record one event in memory and persist it to database tables."""

        fallback = self._run_identities.get(run_id, self._default_identity)
        event_payload, identity = enrich_event_payload(payload, fallback)
        event_payload = enrich_trace_payload(run_id, event_payload)
        self._run_identities[run_id] = identity
        await super().record(run_id, event_type, event_payload)
        if event_type == "observation":
            step_index = event_payload.get("step_index")
            if isinstance(step_index, int):
                self._observations[(run_id, step_index)] = event_payload.get("observation", {})

        with self.session_factory() as session:
            recorded_at = datetime.now(tz=UTC)
            self._ensure_run(session, run_id, event_type, event_payload)
            self._ensure_round_span(
                session,
                run_id,
                event_type,
                event_payload,
                recorded_at=recorded_at,
            )
            if event_type in _RUN_TERMINAL_EVENTS:
                self._finalize_open_round_spans(session, run_id, recorded_at=recorded_at)
            session.add(
                TrajectoryEventRecord(
                    run_id=run_id,
                    event_type=event_type,
                    payload=event_payload,
                    step_index=step_index_from_payload(event_payload),
                    trace_id=str(event_payload["trace_id"]),
                    span_id=str(event_payload["span_id"]),
                    task_id=identity.task_id,
                    agent_id=identity.agent_id,
                )
            )
            self._record_specialized_table(session, run_id, event_type, event_payload)
            session.commit()
        if self._event_callback is not None:
            await self._event_callback(run_id, event_type, event_payload)

    def _ensure_run(
        self, session: Any, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Create or update the run summary row for one event."""

        run = session.get(RunRecord, run_id)
        if run is None:
            task_id = (
                payload.get("task_id") or payload.get("task_spec", {}).get("task_id") or "unknown"
            )
            run = RunRecord(
                id=run_id,
                trace_id=trace_id_for_run(run_id),
                root_span_id=root_span_id_for_run(run_id),
                task_id=str(task_id),
                status="running",
                task_spec=payload.get("task_spec", {}),
            )
            session.add(run)
        else:
            run.trace_id = trace_id_for_run(run_id)
            run.root_span_id = root_span_id_for_run(run_id)

        if event_type == "run_started":
            run.status = "running"
            run.task_id = str(payload.get("task_id", run.task_id))
            run.task_spec = payload.get("task_spec", run.task_spec)
        elif event_type == "run_finished":
            if payload.get("stop_reason") == "agent_submitted_for_external_evaluation":
                run.status = "awaiting_human_review"
            elif payload.get("stop_reason") == "agent_finished_unverified":
                run.status = "completed_unverified"
            else:
                run.status = "terminated" if payload.get("terminated") else "completed"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_failed":
            run.status = "failed"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_interrupted":
            run.status = "cancelled"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_model_timeout":
            run.status = "model_timeout"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_task_timeout":
            run.status = "task_timeout"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_runtime_error":
            run.status = "runtime_error"
            run.finished_at = datetime.now(tz=UTC)
        elif event_type == "run_verification_inconclusive":
            if run.status not in _OPERATIONAL_TERMINAL_STATUSES:
                run.status = (
                    "awaiting_human_review"
                    if self._has_pending_human_review(session, run_id)
                    else "verification_inconclusive"
                )
                run.finished_at = datetime.now(tz=UTC)
        elif event_type == "verifier_result":
            if run.status in _OPERATIONAL_TERMINAL_STATUSES:
                return
            if payload.get("authoritative") is False:
                if self._has_pending_human_review(session, run_id):
                    run.status = "awaiting_human_review"
                    run.finished_at = datetime.now(tz=UTC)
                return
            if self._has_pending_human_review(session, run_id):
                run.status = "awaiting_human_review"
                run.finished_at = datetime.now(tz=UTC)
                return
            verifier = (
                payload.get("verifier") if isinstance(payload.get("verifier"), dict) else payload
            )
            success = verifier.get("success") if isinstance(verifier, dict) else None
            inconclusive = verifier.get("inconclusive") if isinstance(verifier, dict) else None
            if success is True:
                run.status = "succeeded"
                run.finished_at = datetime.now(tz=UTC)
            elif inconclusive:
                run.status = "verification_inconclusive"
                run.finished_at = datetime.now(tz=UTC)
            elif success is False:
                run.status = "failed"
                run.finished_at = datetime.now(tz=UTC)

    def _record_specialized_table(
        self,
        session: Any,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist event payloads into query-friendly specialized tables."""

        model_call_events = {
            "model_action",
            "agent_plan_model_call",
            "model_error",
            "invalid_action",
            "model_repair_failed",
        }
        if event_type in model_call_events:
            raw_response_payload = payload.get("raw_response")
            raw_response = (
                dict(raw_response_payload) if isinstance(raw_response_payload, dict) else {}
            )
            if event_type == "model_action" and isinstance(payload.get("decision"), dict):
                raw_response["decision"] = payload["decision"]
            session.add(
                ModelCallRecord(
                    run_id=run_id,
                    step_index=int(payload.get("step_index", 0)),
                    trace_id=str(payload["trace_id"]),
                    span_id=str(payload["span_id"]),
                    raw_content=str(payload.get("raw_content") or ""),
                    action=payload.get("action"),
                    usage=payload.get("usage", {}),
                    raw_response=raw_response,
                    source=str(
                        payload.get("source")
                        or ("planner" if event_type == "agent_plan_model_call" else event_type)
                    ),
                )
            )
        elif event_type == "action_result":
            self._upsert_step(session, run_id, payload)
        elif event_type in {"runtime_error", "runtime_action_timeout", "run_runtime_error"}:
            session.add(
                RuntimeErrorRecord(
                    run_id=run_id,
                    step_index=payload.get("step_index"),
                    trace_id=str(payload["trace_id"]),
                    span_id=str(payload["span_id"]),
                    error_type=str(payload.get("error_type", "runtime_error")),
                    message=str(payload.get("message", "")),
                    payload=payload,
                )
            )
        elif event_type in {"creative_evaluation_completed", "creative_evaluation_inconclusive"}:
            self._upsert_creative_evaluation(session, run_id, event_type, payload)
            self._upsert_human_review_from_evaluation(session, run_id, payload)
        elif event_type == "agent_finish_accepted" and payload.get(
            "stop_reason"
        ) == "agent_submitted_for_external_evaluation":
            self._upsert_human_review_from_submission(session, run_id, payload)

    def _upsert_creative_evaluation(
        self,
        session: Any,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Create or replace the latest creative evaluation summary for one run."""

        record = session.scalar(
            select(CreativeEvaluationRecord).where(CreativeEvaluationRecord.run_id == run_id)
        )
        calibration = (
            payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
        )
        trend = payload.get("score_trend") if isinstance(payload.get("score_trend"), list) else []
        scorer_payload = trend[0].get("scorer") if trend and isinstance(trend[0], dict) else {}
        scorer = scorer_payload if isinstance(scorer_payload, dict) else {}
        success = payload.get("success") if isinstance(payload.get("success"), bool) else None
        if payload.get("inconclusive"):
            success = None
        values = {
            "task_id": str(payload.get("task_id") or self._default_identity.task_id or "unknown"),
            "status": "inconclusive" if payload.get("inconclusive") else "completed",
            "prompt": str(payload.get("prompt") or ""),
            "score": _optional_float(payload.get("score")),
            "score_threshold": _optional_float(payload.get("score_threshold")),
            "success": success,
            "scorer": str(scorer.get("name") or "mineclip"),
            "variant": str(scorer["variant"]) if scorer.get("variant") else None,
            "calibration_status": str(calibration.get("status") or "pending"),
            "frame_count": int(payload.get("frame_count") or 0),
            "window_count": int(payload.get("window_count") or 0),
            "result": payload,
        }
        if record is None:
            session.add(CreativeEvaluationRecord(run_id=run_id, **values))
            return
        for key, value in values.items():
            setattr(record, key, value)

    def _upsert_human_review_from_submission(
        self,
        session: Any,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Create the review queue entry when an agent submits a creative result."""

        run = session.get(RunRecord, run_id)
        if run is None:
            return
        record = session.scalar(
            select(HumanReviewRecord).where(HumanReviewRecord.run_id == run_id)
        )
        decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
        summary = str(decision.get("reasoning_summary") or "")
        evidence = {
            "submission": {
                "step_index": payload.get("step_index"),
                "decision": decision,
                "verifier": payload.get("verifier"),
                "action_result": payload.get("action_result"),
            }
        }
        if record is None:
            session.add(
                HumanReviewRecord(
                    run_id=run_id,
                    task_id=run.task_id,
                    task_name=_task_display_name(run.task_spec, run.task_id),
                    status="awaiting_review",
                    submission_summary=summary,
                    evidence=evidence,
                )
            )
            return
        if record.status == "awaiting_review":
            record.submission_summary = summary or record.submission_summary
        record.evidence = {**dict(record.evidence or {}), **evidence}

    def _upsert_human_review_from_evaluation(
        self,
        session: Any,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Attach MineCLIP and media evidence without turning it into a human decision."""

        run = session.get(RunRecord, run_id)
        if run is None:
            return
        record = session.scalar(
            select(HumanReviewRecord).where(HumanReviewRecord.run_id == run_id)
        )
        evidence = dict(record.evidence or {}) if record is not None else {}
        evidence.update(
            {
                "source": payload.get("evidence_source"),
                "final_frame": payload.get("final_frame"),
                "key_frames": payload.get("key_frames") or [],
                "mineclip": {
                    "status": "inconclusive" if payload.get("inconclusive") else "completed",
                    "score": payload.get("score"),
                    "score_threshold": payload.get("score_threshold"),
                    "calibration": payload.get("calibration"),
                    "window_count": payload.get("window_count"),
                    "frame_count": payload.get("frame_count"),
                    "reason": payload.get("reason"),
                },
            }
        )
        if record is None:
            session.add(
                HumanReviewRecord(
                    run_id=run_id,
                    task_id=run.task_id,
                    task_name=_task_display_name(run.task_spec, run.task_id),
                    status="awaiting_review",
                    submission_summary="",
                    evidence=evidence,
                )
            )
        else:
            record.evidence = evidence
        if (
            (record is None or record.status == "awaiting_review")
            and run.status not in _OPERATIONAL_TERMINAL_STATUSES
        ):
            run.status = "awaiting_human_review"
            run.finished_at = run.finished_at or datetime.now(tz=UTC)

    @staticmethod
    def _has_pending_human_review(session: Any, run_id: str) -> bool:
        """Return whether a creative run still awaits an authoritative human decision."""

        return (
            session.scalar(
                select(HumanReviewRecord.id).where(
                    HumanReviewRecord.run_id == run_id,
                    HumanReviewRecord.status == "awaiting_review",
                )
            )
            is not None
        )

    def _upsert_step(self, session: Any, run_id: str, payload: dict[str, Any]) -> None:
        """Create or update the step row for an action result event."""

        step_index = int(payload.get("step_index", 0))
        step = session.scalar(
            select(StepRecord).where(
                StepRecord.run_id == run_id,
                StepRecord.step_index == step_index,
            )
        )
        if step is None:
            step = StepRecord(
                run_id=run_id,
                step_index=step_index,
                trace_id=str(payload["trace_id"]),
                span_id=str(payload["span_id"]),
                observation={},
                action={},
                action_result={},
            )
            session.add(step)
        else:
            step.trace_id = str(payload["trace_id"])
            step.span_id = str(payload["span_id"])
        step.observation = self._observations.get((run_id, step_index), {})
        step.action = payload.get("action", {})
        step.action_result = payload.get("result", {})

    def _ensure_round_span(
        self,
        session: Any,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        recorded_at: datetime,
    ) -> RoundSpanRecord | None:
        """Create or advance the durable span for one round-scoped event."""

        step_index = step_index_from_payload(payload)
        if step_index is None:
            return None
        span = session.scalar(
            select(RoundSpanRecord).where(
                RoundSpanRecord.run_id == run_id,
                RoundSpanRecord.step_index == step_index,
            )
        )
        if span is None:
            span = RoundSpanRecord(
                run_id=run_id,
                step_index=step_index,
                trace_id=trace_id_for_run(run_id),
                span_id=span_id_for_round(run_id, step_index),
                parent_span_id=root_span_id_for_run(run_id),
                status="active",
                started_at=recorded_at,
                attributes={},
            )
            session.add(span)
        elif event_type == "observation" and span.status in {"incomplete", "interrupted"}:
            span.status = "active"
            span.finished_at = None

        attributes = dict(span.attributes or {})
        attributes["event_count"] = int(attributes.get("event_count") or 0) + 1
        attributes["last_event_type"] = event_type
        span.attributes = attributes

        terminal_status = _round_terminal_status(event_type, payload)
        if terminal_status is not None and _round_status_rank(terminal_status) >= _round_status_rank(
            span.status
        ):
            span.status = terminal_status
            span.finished_at = recorded_at
        return span

    @staticmethod
    def _finalize_open_round_spans(
        session: Any,
        run_id: str,
        *,
        recorded_at: datetime,
    ) -> None:
        """Close any round that ended when the enclosing run terminated early."""

        spans = session.scalars(
            select(RoundSpanRecord).where(
                RoundSpanRecord.run_id == run_id,
                RoundSpanRecord.status == "active",
            )
        ).all()
        for span in spans:
            span.status = "interrupted"
            span.finished_at = recorded_at


def _optional_float(value: Any) -> float | None:
    """Normalize optional numeric audit fields for SQL persistence."""

    return float(value) if isinstance(value, (int, float)) else None


def _task_display_name(task_spec: dict[str, Any], fallback: str) -> str:
    """Choose the most useful persisted title for a human creative-task reviewer."""

    for field in ("task_name", "title", "prompt", "goal", "description"):
        value = task_spec.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    verifier = task_spec.get("verifier")
    if isinstance(verifier, dict):
        prompt = verifier.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            return prompt.strip()
    return fallback


_OPERATIONAL_TERMINAL_STATUSES = {
    "cancelled",
    "model_timeout",
    "runtime_error",
    "task_timeout",
}

_RUN_TERMINAL_EVENTS = {
    "run_failed",
    "run_finished",
    "run_interrupted",
    "run_model_timeout",
    "run_runtime_error",
    "run_task_timeout",
    "run_verification_inconclusive",
}


def _round_terminal_status(event_type: str, payload: dict[str, Any]) -> str | None:
    """Classify events that close one round span."""

    if event_type == "action_result":
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        return "error" if result.get("ok") is False else "ok"
    if event_type in {"runtime_error", "runtime_action_timeout"}:
        return "error"
    return None


def _round_status_rank(status: str) -> int:
    """Order span states so later non-terminal events cannot reopen failures."""

    return {
        "active": 0,
        "incomplete": 1,
        "interrupted": 1,
        "ok": 2,
        "error": 3,
    }.get(status, 0)
