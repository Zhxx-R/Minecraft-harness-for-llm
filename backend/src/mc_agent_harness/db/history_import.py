from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import quote

from sqlalchemy import Engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from mc_agent_harness.db.models import (
    CreativeEvaluationRecord,
    HumanReviewRecord,
    LearningCandidateRecord,
    ModelCallRecord,
    RoundSpanRecord,
    RunRecord,
    RuntimeErrorRecord,
    SkillRecord,
    StepRecord,
    TaskMemoryRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.db.session import create_database_engine
from mc_agent_harness.observability.tracing import (
    root_span_id_for_run,
    span_id_for_round,
    trace_id_for_run,
)


_RUN_SCOPED_TABLES = (
    "round_spans",
    "steps",
    "trajectory_events",
    "model_calls",
    "runtime_errors",
    "creative_evaluations",
    "human_reviews",
)
_REQUIRED_TARGET_TABLES = {
    "runs",
    "round_spans",
    "steps",
    "trajectory_events",
    "model_calls",
    "runtime_errors",
    "creative_evaluations",
    "human_reviews",
}
_TERMINAL_STATUSES = {
    "awaiting_human_review",
    "cancelled",
    "completed",
    "completed_unverified",
    "failed",
    "interrupted",
    "model_timeout",
    "runtime_error",
    "succeeded",
    "task_timeout",
    "terminated",
    "verification_inconclusive",
}


@dataclass(frozen=True)
class RunSource:
    """One historical run row and the evidence available beside it."""

    run_id: str
    source_path: Path
    relative_source: str
    run_row: dict[str, Any]
    evidence_counts: dict[str, int]
    evidence_bytes: int

    @property
    def target_run_id(self) -> str:
        """Return a deterministic ID compatible with the shared database schema."""

        return _bounded_run_id(self.run_id)

    @property
    def score(self) -> tuple[int, int, int, int, str]:
        """Rank sources by evidence diversity, volume, lifecycle, and recency."""

        populated_tables = sum(count > 0 for count in self.evidence_counts.values())
        evidence_rows = sum(self.evidence_counts.values())
        status = str(self.run_row.get("status") or "")
        terminal = int(
            status in _TERMINAL_STATUSES or self.run_row.get("finished_at") is not None
        )
        updated = str(
            self.run_row.get("updated_at")
            or self.run_row.get("finished_at")
            or self.run_row.get("created_at")
            or ""
        )
        return populated_tables, evidence_rows, terminal, self.evidence_bytes, updated


@dataclass
class HistoryImportStats:
    """Machine-readable summary emitted by a historical audit import."""

    dry_run: bool
    runs_root: str
    scanned_databases: int = 0
    source_databases_with_runs: int = 0
    candidate_run_rows: int = 0
    unique_runs: int = 0
    duplicate_run_sources: int = 0
    selected_source_databases: int = 0
    existing_runs_skipped: int = 0
    runs_normalized_from_running: int = 0
    rows_imported: dict[str, int] = field(default_factory=dict)
    rows_skipped: dict[str, int] = field(default_factory=dict)
    source_errors: list[dict[str, str]] = field(default_factory=list)

    def imported(self, table: str, count: int = 1) -> None:
        """Increment an imported-row counter."""

        self.rows_imported[table] = self.rows_imported.get(table, 0) + count

    def skipped(self, table: str, count: int = 1) -> None:
        """Increment a skipped-row counter."""

        self.rows_skipped[table] = self.rows_skipped.get(table, 0) + count

    def as_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable representation."""

        return {
            "dry_run": self.dry_run,
            "runs_root": self.runs_root,
            "scanned_databases": self.scanned_databases,
            "source_databases_with_runs": self.source_databases_with_runs,
            "candidate_run_rows": self.candidate_run_rows,
            "unique_runs": self.unique_runs,
            "duplicate_run_sources": self.duplicate_run_sources,
            "selected_source_databases": self.selected_source_databases,
            "existing_runs_skipped": self.existing_runs_skipped,
            "runs_normalized_from_running": self.runs_normalized_from_running,
            "rows_imported": dict(sorted(self.rows_imported.items())),
            "rows_skipped": dict(sorted(self.rows_skipped.items())),
            "source_errors": self.source_errors,
        }


def import_historical_audits(
    *,
    runs_root: Path,
    database_url: str | None = None,
    dry_run: bool = False,
    normalize_running: bool = True,
    engine: Engine | None = None,
) -> HistoryImportStats:
    """Import the best copy of each historical SQLite run into the shared database.

    Existing target run IDs are deliberately left untouched. Related records are
    imported only for newly inserted runs, making repeated execution idempotent.
    """

    root = runs_root.expanduser().resolve()
    stats = HistoryImportStats(dry_run=dry_run, runs_root=str(root))
    target_engine = engine or create_database_engine(database_url)
    source_paths = discover_audit_databases(
        root,
        excluded_path=_sqlite_database_path(database_url) if database_url else None,
    )
    stats.scanned_databases = len(source_paths)
    selected = select_best_run_sources(source_paths, root, stats)
    stats.unique_runs = len(selected)
    stats.selected_source_databases = len({candidate.source_path for candidate in selected.values()})

    target_tables = _validate_target_schema(target_engine)
    with Session(target_engine, expire_on_commit=False) as session:
        target_ids_by_source_id = {
            run_id: candidate.target_run_id for run_id, candidate in selected.items()
        }
        if len(set(target_ids_by_source_id.values())) != len(target_ids_by_source_id):
            raise RuntimeError("Historical run IDs collide after normalization.")
        run_ids = sorted(target_ids_by_source_id.values())
        existing_run_ids = set(
            session.scalars(select(RunRecord.id).where(RunRecord.id.in_(run_ids))).all()
            if run_ids
            else []
        )
        stats.existing_runs_skipped = len(existing_run_ids)
        candidates = [
            candidate
            for candidate in selected.values()
            if candidate.target_run_id not in existing_run_ids
        ]
        imported_by_source: dict[Path, list[RunSource]] = defaultdict(list)

        for candidate in candidates:
            run = _build_run_record(candidate, normalize_running=normalize_running)
            if str(candidate.run_row.get("status") or "running") == "running" and normalize_running:
                stats.runs_normalized_from_running += 1
            session.add(run)
            imported_by_source[candidate.source_path].append(candidate)
            stats.imported("runs")
        session.flush()

        for source_path, source_candidates in sorted(
            imported_by_source.items(), key=lambda item: str(item[0])
        ):
            relative_source = source_candidates[0].relative_source
            try:
                with _open_source(source_path) as source:
                    tables = _source_tables(source)
                    for candidate in source_candidates:
                        _import_run_children(
                            session,
                            source,
                            tables,
                            candidate,
                            stats,
                        )
                    _import_auxiliary_records(
                        session,
                        source,
                        tables,
                        source_candidates,
                        relative_source,
                        target_tables,
                        stats,
                    )
            except (OSError, sqlite3.DatabaseError, ValueError) as exc:
                stats.source_errors.append(
                    {"source_database": relative_source, "error": str(exc)}
                )
                raise

        if dry_run:
            session.rollback()
        else:
            session.commit()
    return stats


def discover_audit_databases(
    runs_root: Path,
    *,
    excluded_path: Path | None = None,
) -> list[Path]:
    """Return all historical SQLite audit databases under ``runs_root``."""

    root = runs_root.expanduser().resolve()
    excluded = excluded_path.expanduser().resolve() if excluded_path is not None else None
    return sorted(
        (
            path.resolve()
            for path in root.rglob("*.sqlite3")
            if path.is_file() and (excluded is None or path.resolve() != excluded)
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def select_best_run_sources(
    source_paths: Iterable[Path],
    runs_root: Path,
    stats: HistoryImportStats | None = None,
) -> dict[str, RunSource]:
    """Choose the most evidence-complete source database for every run ID."""

    root = runs_root.expanduser().resolve()
    candidates_by_run: dict[str, list[RunSource]] = defaultdict(list)
    for source_path in source_paths:
        relative_source = source_path.relative_to(root).as_posix()
        try:
            with _open_source(source_path) as source:
                tables = _source_tables(source)
                if "runs" not in tables:
                    continue
                rows = [dict(row) for row in source.execute('SELECT * FROM "runs"')]
                if rows and stats is not None:
                    stats.source_databases_with_runs += 1
                    stats.candidate_run_rows += len(rows)
                for row in rows:
                    run_id = str(row.get("id") or "")
                    if not run_id:
                        continue
                    counts: dict[str, int] = {}
                    evidence_bytes = _row_size(row)
                    for table in _RUN_SCOPED_TABLES:
                        if table not in tables:
                            counts[table] = 0
                            continue
                        evidence_rows = _fetch_run_rows(source, table, run_id)
                        counts[table] = len(evidence_rows)
                        evidence_bytes += sum(_row_size(item) for item in evidence_rows)
                    candidates_by_run[run_id].append(
                        RunSource(
                            run_id=run_id,
                            source_path=source_path,
                            relative_source=relative_source,
                            run_row=row,
                            evidence_counts=counts,
                            evidence_bytes=evidence_bytes,
                        )
                    )
        except (OSError, sqlite3.DatabaseError) as exc:
            if stats is not None:
                stats.source_errors.append(
                    {"source_database": relative_source, "error": str(exc)}
                )

    selected: dict[str, RunSource] = {}
    for run_id, candidates in candidates_by_run.items():
        if stats is not None and len(candidates) > 1:
            stats.duplicate_run_sources += len(candidates) - 1
        best_score = max(candidate.score for candidate in candidates)
        tied = [candidate for candidate in candidates if candidate.score == best_score]
        # Prefer a lexically stable source when evidence scores tie.
        selected[run_id] = min(tied, key=lambda candidate: candidate.relative_source)
    return selected


def _validate_target_schema(engine: Engine) -> set[str]:
    tables = set(inspect(engine).get_table_names())
    missing = sorted(_REQUIRED_TARGET_TABLES - tables)
    if missing:
        raise RuntimeError(
            "Target database is not migrated; missing tables: " + ", ".join(missing)
        )
    return tables


def _build_run_record(candidate: RunSource, *, normalize_running: bool) -> RunRecord:
    row = candidate.run_row
    original_status = str(row.get("status") or "running")
    status = "interrupted" if normalize_running and original_status == "running" else original_status
    provenance_extra = {
        "original_status": original_status,
        "original_resumed_from_checkpoint_id": row.get("resumed_from_checkpoint_id"),
    }
    if candidate.target_run_id != candidate.run_id:
        provenance_extra["original_run_id"] = candidate.run_id
    task_spec = _with_provenance(
        row.get("task_spec"),
        candidate.relative_source,
        "runs",
        row.get("id"),
        extra=provenance_extra,
    )
    values: dict[str, Any] = {
        "id": candidate.target_run_id,
        "trace_id": trace_id_for_run(candidate.target_run_id),
        "root_span_id": root_span_id_for_run(candidate.target_run_id),
        "task_id": str(row.get("task_id") or "unknown"),
        "status": status,
        "task_spec": task_spec,
        "resumed_from_checkpoint_id": None,
    }
    values.update(_timestamp_values(row, include=("created_at", "updated_at", "started_at")))
    finished_at = _parse_datetime(row.get("finished_at"))
    if finished_at is not None:
        values["finished_at"] = finished_at
    return RunRecord(**values)


def _import_run_children(
    session: Session,
    source: sqlite3.Connection,
    tables: set[str],
    candidate: RunSource,
    stats: HistoryImportStats,
) -> None:
    source_run_id = candidate.run_id
    run_id = candidate.target_run_id
    relative_source = candidate.relative_source
    trace_id = trace_id_for_run(run_id)
    root_span_id = root_span_id_for_run(run_id)
    round_aggregates: dict[int, dict[str, Any]] = {}
    source_round_rows = _fetch_run_rows_if_present(
        source,
        tables,
        "round_spans",
        source_run_id,
    )
    step_rows = _fetch_run_rows_if_present(source, tables, "steps", source_run_id)
    event_rows = _fetch_run_rows_if_present(
        source,
        tables,
        "trajectory_events",
        source_run_id,
    )
    model_call_rows = _fetch_run_rows_if_present(
        source,
        tables,
        "model_calls",
        source_run_id,
    )
    runtime_error_rows = _fetch_run_rows_if_present(
        source,
        tables,
        "runtime_errors",
        source_run_id,
    )

    for row in source_round_rows:
        step_index = _round_step_index(row.get("step_index"))
        if step_index is None:
            continue
        _touch_imported_round(
            round_aggregates,
            step_index,
            row,
            status=_round_status(row.get("status")),
            source_span=True,
        )

    for row in step_rows:
        step_index = int(row.get("step_index") or 0)
        values: dict[str, Any] = {
            "run_id": run_id,
            "step_index": step_index,
            "trace_id": trace_id,
            "span_id": span_id_for_round(run_id, step_index),
            "observation": _json_dict(row.get("observation")),
            "action": _json_dict(row.get("action")),
            "action_result": _with_provenance(
                row.get("action_result"),
                relative_source,
                "steps",
                row.get("id"),
            ),
        }
        values.update(_timestamp_values(row))
        session.add(StepRecord(**values))
        stats.imported("steps")
        _touch_imported_round(
            round_aggregates,
            step_index,
            row,
            status=_result_status(_json_dict(row.get("action_result"))),
            has_step_record=True,
        )

    for row in event_rows:
        payload = _with_provenance(
            row.get("payload"),
            relative_source,
            "trajectory_events",
            row.get("id"),
        )
        step_index = _round_step_index(row.get("step_index"))
        if step_index is None:
            step_index = _round_step_index(payload.get("step_index"))
        span_id = root_span_id
        if step_index is not None:
            payload["step_index"] = step_index
            span_id = span_id_for_round(run_id, step_index)
        values = {
            "run_id": run_id,
            "event_type": str(row.get("event_type") or "unknown"),
            "payload": payload,
            "step_index": step_index,
            "trace_id": trace_id,
            "span_id": span_id,
            "task_id": _optional_string(row.get("task_id")),
            "agent_id": _optional_string(row.get("agent_id")),
        }
        values.update(_timestamp_values(row))
        session.add(TrajectoryEventRecord(**values))
        stats.imported("trajectory_events")
        if step_index is not None:
            _touch_imported_round(
                round_aggregates,
                step_index,
                row,
                status=_event_terminal_status(str(row.get("event_type") or "unknown"), payload),
                event_count=1,
            )

    for row in model_call_rows:
        step_index = int(row.get("step_index") or 0)
        values = {
            "run_id": run_id,
            "step_index": step_index,
            "trace_id": trace_id,
            "span_id": span_id_for_round(run_id, step_index),
            "raw_content": str(row.get("raw_content") or ""),
            "action": _optional_json_dict(row.get("action")),
            "usage": _json_dict(row.get("usage")),
            "raw_response": _with_provenance(
                row.get("raw_response"),
                relative_source,
                "model_calls",
                row.get("id"),
            ),
            "source": str(row.get("source") or "model"),
        }
        values.update(_timestamp_values(row))
        session.add(ModelCallRecord(**values))
        stats.imported("model_calls")
        _touch_imported_round(
            round_aggregates,
            step_index,
            row,
            model_call_count=1,
        )

    for row in runtime_error_rows:
        step_index = _round_step_index(row.get("step_index"))
        payload = _with_provenance(
            row.get("payload"),
            relative_source,
            "runtime_errors",
            row.get("id"),
        )
        if step_index is None:
            step_index = _round_step_index(payload.get("step_index"))
        span_id = root_span_id
        if step_index is not None:
            payload["step_index"] = step_index
            span_id = span_id_for_round(run_id, step_index)
        values = {
            "run_id": run_id,
            "step_index": step_index,
            "trace_id": trace_id,
            "span_id": span_id,
            "error_type": str(row.get("error_type") or "runtime_error"),
            "message": str(row.get("message") or ""),
            "payload": payload,
        }
        values.update(_timestamp_values(row))
        session.add(RuntimeErrorRecord(**values))
        stats.imported("runtime_errors")
        if step_index is not None:
            _touch_imported_round(
                round_aggregates,
                step_index,
                row,
                status="error",
                runtime_error_count=1,
            )

    run = session.get(RunRecord, run_id)
    if run is None:
        raise RuntimeError(f"Imported run disappeared before round-span creation: {run_id}")
    for step_index, aggregate in sorted(round_aggregates.items()):
        status = str(aggregate["status"])
        if status == "active" and run.status != "running":
            status = "incomplete"
        started_at = aggregate["started_at"] or run.started_at
        finished_at = (
            None
            if status == "active"
            else aggregate["last_at"] or run.finished_at or started_at
        )
        session.add(
            RoundSpanRecord(
                run_id=run_id,
                step_index=step_index,
                trace_id=trace_id,
                span_id=span_id_for_round(run_id, step_index),
                parent_span_id=root_span_id,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                attributes={
                    "historical_import": True,
                    "source_database": relative_source,
                    "source_run_id": source_run_id,
                    "source_span_present": bool(aggregate["source_span"]),
                    "event_count": int(aggregate["event_count"]),
                    "model_call_count": int(aggregate["model_call_count"]),
                    "runtime_error_count": int(aggregate["runtime_error_count"]),
                    "has_step_record": bool(aggregate["has_step_record"]),
                },
                created_at=started_at,
                updated_at=aggregate["last_at"] or started_at,
            )
        )
        stats.imported("round_spans")

    creative_rows = _fetch_run_rows_if_present(
        source, tables, "creative_evaluations", source_run_id
    )
    if creative_rows:
        row = _latest_row(creative_rows)
        values = {
            "run_id": run_id,
            "task_id": str(row.get("task_id") or candidate.run_row.get("task_id") or "unknown"),
            "status": str(row.get("status") or "completed"),
            "prompt": str(row.get("prompt") or ""),
            "score": _optional_float(row.get("score")),
            "score_threshold": _optional_float(row.get("score_threshold")),
            "success": _optional_bool(row.get("success")),
            "scorer": str(row.get("scorer") or "mineclip"),
            "variant": _optional_string(row.get("variant")),
            "calibration_status": str(row.get("calibration_status") or "pending"),
            "frame_count": int(row.get("frame_count") or 0),
            "window_count": int(row.get("window_count") or 0),
            "result": _with_provenance(
                row.get("result"),
                relative_source,
                "creative_evaluations",
                row.get("id"),
            ),
        }
        values.update(_timestamp_values(row))
        session.add(CreativeEvaluationRecord(**values))
        stats.imported("creative_evaluations")
        stats.skipped("creative_evaluations", len(creative_rows) - 1)

    review_rows = _fetch_run_rows_if_present(
        source, tables, "human_reviews", source_run_id
    )
    if review_rows:
        row = _latest_row(review_rows)
        values = {
            "run_id": run_id,
            "task_id": str(row.get("task_id") or candidate.run_row.get("task_id") or "unknown"),
            "task_name": str(
                row.get("task_name") or candidate.run_row.get("task_id") or "unknown"
            ),
            "status": str(row.get("status") or "awaiting_review"),
            "submission_summary": str(row.get("submission_summary") or ""),
            "evidence": _with_provenance(
                row.get("evidence"),
                relative_source,
                "human_reviews",
                row.get("id"),
            ),
            "reviewer_id": _optional_string(row.get("reviewer_id")),
            "decision": _optional_string(row.get("decision")),
            "reason_codes": _json_list(row.get("reason_codes")),
            "notes": str(row.get("notes") or ""),
            "version": int(row.get("version") or 1),
        }
        values.update(_timestamp_values(row))
        submitted_at = _parse_datetime(row.get("submitted_at"))
        if submitted_at is not None:
            values["submitted_at"] = submitted_at
        decided_at = _parse_datetime(row.get("decided_at"))
        if decided_at is not None:
            values["decided_at"] = decided_at
        session.add(HumanReviewRecord(**values))
        stats.imported("human_reviews")
        stats.skipped("human_reviews", len(review_rows) - 1)


def _import_auxiliary_records(
    session: Session,
    source: sqlite3.Connection,
    tables: set[str],
    candidates: list[RunSource],
    relative_source: str,
    target_tables: set[str],
    stats: HistoryImportStats,
) -> None:
    imported_run_ids = {candidate.run_id for candidate in candidates}
    target_run_ids = {
        candidate.run_id: candidate.target_run_id for candidate in candidates
    }
    imported_task_ids = {
        str(candidate.run_row.get("task_id") or "unknown") for candidate in candidates
    }

    if "skills" in tables and "skills" in target_tables:
        existing_skills = set(session.execute(select(SkillRecord.name, SkillRecord.version)).all())
        for row in _fetch_all(source, "skills"):
            original_source_run_id = _optional_string(row.get("source_run_id"))
            if original_source_run_id is not None and original_source_run_id not in imported_run_ids:
                continue
            key = (str(row.get("name") or ""), str(row.get("version") or "1"))
            if not key[0] or key in existing_skills:
                stats.skipped("skills")
                continue
            values = {
                "name": key[0],
                "version": key[1],
                "status": str(row.get("status") or "candidate"),
                "spec": _with_provenance(
                    row.get("spec"),
                    relative_source,
                    "skills",
                    row.get("id"),
                    extra={"original_source_run_id": original_source_run_id},
                ),
                "source_run_id": (
                    target_run_ids[original_source_run_id]
                    if original_source_run_id in imported_run_ids
                    else None
                ),
            }
            values.update(_timestamp_values(row))
            session.add(SkillRecord(**values))
            existing_skills.add(key)
            stats.imported("skills")

    if "learning_candidates" in tables and "learning_candidates" in target_tables:
        existing_signatures = set(session.scalars(select(LearningCandidateRecord.signature)).all())
        for row in _fetch_all(source, "learning_candidates"):
            signature = str(row.get("signature") or "")
            source_run_ids = _json_string_list(row.get("source_run_ids"))
            recovery_run_ids = _json_string_list(row.get("recovery_run_ids"))
            referenced_runs = set(source_run_ids) | set(recovery_run_ids)
            if referenced_runs and not referenced_runs.intersection(imported_run_ids):
                continue
            if not signature or signature in existing_signatures:
                stats.skipped("learning_candidates")
                continue
            values = {
                "signature": signature,
                "scope_key": str(row.get("scope_key") or "default"),
                "kind": str(row.get("kind") or "failure_pattern"),
                "status": str(row.get("status") or "candidate"),
                "hypothesis": str(row.get("hypothesis") or ""),
                "failure_status": str(row.get("failure_status") or "unknown"),
                "action_type": str(row.get("action_type") or "unknown"),
                "target": _optional_string(row.get("target")),
                "support_count": int(row.get("support_count") or 1),
                "recovery_count": int(row.get("recovery_count") or 0),
                "contradiction_count": int(row.get("contradiction_count") or 0),
                "confidence": float(row.get("confidence") or 0.2),
                "evidence": _with_provenance(
                    row.get("evidence"),
                    relative_source,
                    "learning_candidates",
                    row.get("id"),
                ),
                "knowledge_refs": _json_dict_list(row.get("knowledge_refs")),
                "source_run_ids": source_run_ids,
                "recovery_run_ids": recovery_run_ids,
            }
            values.update(_timestamp_values(row))
            session.add(LearningCandidateRecord(**values))
            existing_signatures.add(signature)
            stats.imported("learning_candidates")

    if "task_memories" in tables and "task_memories" in target_tables:
        existing_memories = set(
            session.execute(
                select(
                    TaskMemoryRecord.task_id,
                    TaskMemoryRecord.namespace,
                    TaskMemoryRecord.content,
                )
            ).all()
        )
        for row in _fetch_all(source, "task_memories"):
            task_id = str(row.get("task_id") or "unknown")
            namespace = str(row.get("namespace") or "default")
            content = str(row.get("content") or "")
            key = (task_id, namespace, content)
            if task_id not in imported_task_ids:
                continue
            if key in existing_memories:
                stats.skipped("task_memories")
                continue
            values = {
                "task_id": task_id,
                "namespace": namespace,
                "content": content,
                "memory_metadata": _with_provenance(
                    row.get("memory_metadata"),
                    relative_source,
                    "task_memories",
                    row.get("id"),
                ),
            }
            values.update(_timestamp_values(row))
            session.add(TaskMemoryRecord(**values))
            existing_memories.add(key)
            stats.imported("task_memories")


def _touch_imported_round(
    rounds: dict[int, dict[str, Any]],
    step_index: int,
    row: dict[str, Any],
    *,
    status: str | None = None,
    event_count: int = 0,
    model_call_count: int = 0,
    runtime_error_count: int = 0,
    has_step_record: bool = False,
    source_span: bool = False,
) -> None:
    """Merge one historical child row into its canonical target round span."""

    started_at = (
        _parse_datetime(row.get("started_at"))
        or _parse_datetime(row.get("created_at"))
    )
    last_at = (
        _parse_datetime(row.get("finished_at"))
        or _parse_datetime(row.get("updated_at"))
        or started_at
    )
    aggregate = rounds.setdefault(
        step_index,
        {
            "status": "active",
            "started_at": started_at,
            "last_at": last_at,
            "event_count": 0,
            "model_call_count": 0,
            "runtime_error_count": 0,
            "has_step_record": False,
            "source_span": False,
        },
    )
    if started_at is not None and (
        aggregate["started_at"] is None or started_at < aggregate["started_at"]
    ):
        aggregate["started_at"] = started_at
    if last_at is not None and (
        aggregate["last_at"] is None or last_at > aggregate["last_at"]
    ):
        aggregate["last_at"] = last_at
    if status is not None and _round_status_rank(status) >= _round_status_rank(
        str(aggregate["status"])
    ):
        aggregate["status"] = status
    aggregate["event_count"] += event_count
    aggregate["model_call_count"] += model_call_count
    aggregate["runtime_error_count"] += runtime_error_count
    aggregate["has_step_record"] = aggregate["has_step_record"] or has_step_record
    aggregate["source_span"] = aggregate["source_span"] or source_span


def _event_terminal_status(event_type: str, payload: dict[str, Any]) -> str | None:
    """Return a terminal round status when one historical event closes its round."""

    if event_type == "action_result":
        return _result_status(_json_dict(payload.get("result")))
    if event_type in {"runtime_error", "runtime_action_timeout"}:
        return "error"
    return None


def _result_status(result: dict[str, Any]) -> str:
    """Classify an action result using the same status vocabulary as live tracing."""

    return "error" if result.get("ok") is False else "ok"


def _round_status(value: Any) -> str:
    """Normalize a source round status without trusting unknown historical labels."""

    normalized = str(value or "active").strip().lower()
    return (
        normalized
        if normalized in {"active", "incomplete", "interrupted", "ok", "error"}
        else "active"
    )


def _round_status_rank(status: str) -> int:
    """Order imported round states so errors cannot be overwritten by weaker evidence."""

    return {
        "active": 0,
        "incomplete": 1,
        "interrupted": 1,
        "ok": 2,
        "error": 3,
    }.get(status, 0)


def _round_step_index(value: Any) -> int | None:
    """Return one valid non-negative historical round index."""

    if value is None or isinstance(value, bool):
        return None
    try:
        step_index = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return step_index if step_index >= 0 else None


@contextmanager
def _open_source(path: Path) -> Iterator[sqlite3.Connection]:
    encoded = quote(str(path.resolve()), safe="/")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _source_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _fetch_run_rows_if_present(
    connection: sqlite3.Connection,
    tables: set[str],
    table: str,
    run_id: str,
) -> list[dict[str, Any]]:
    if table not in tables:
        return []
    return _fetch_run_rows(connection, table, run_id)


def _fetch_run_rows(
    connection: sqlite3.Connection,
    table: str,
    run_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f'SELECT * FROM "{table}" WHERE run_id = ? ORDER BY id',
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _fetch_all(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            str(row.get("updated_at") or row.get("created_at") or ""),
            int(row.get("id") or 0),
        ),
    )


def _row_size(row: dict[str, Any]) -> int:
    return sum(
        len(value)
        if isinstance(value, (str, bytes))
        else len(json.dumps(value, ensure_ascii=False, default=str))
        for value in row.values()
        if value is not None
    )


def _with_provenance(
    value: Any,
    relative_source: str,
    source_table: str,
    source_row_id: Any,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _json_dict(value)
    provenance: dict[str, Any] = {
        "source_database": relative_source,
        "source_table": source_table,
    }
    if source_row_id is not None:
        provenance["source_row_id"] = source_row_id
    if extra:
        provenance.update(extra)
    previous = payload.get("_historical_import")
    if previous is not None:
        provenance["upstream"] = previous
    payload["_historical_import"] = provenance
    return payload


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, {})
    if isinstance(parsed, dict):
        return dict(parsed)
    return {"_legacy_value": parsed}


def _optional_json_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _json_dict(value)


def _json_list(value: Any) -> list[Any]:
    parsed = _json_value(value, [])
    return list(parsed) if isinstance(parsed, list) else [parsed]


def _json_string_list(value: Any) -> list[str]:
    return [str(item) for item in _json_list(value)]


def _json_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in _json_list(value) if isinstance(item, dict)]


def _timestamp_values(
    row: dict[str, Any],
    *,
    include: tuple[str, ...] = ("created_at", "updated_at"),
) -> dict[str, datetime]:
    values: dict[str, datetime] = {}
    for column in include:
        parsed = _parse_datetime(row.get(column))
        if parsed is not None:
            values[column] = parsed
    return values


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no"}
    return bool(value)


def _sqlite_database_path(database_url: str | None) -> Path | None:
    if not database_url:
        return None
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def _bounded_run_id(run_id: str) -> str:
    """Keep historical IDs readable while satisfying ``runs.id VARCHAR(64)``."""

    if len(run_id) <= 64:
        return run_id
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
    prefix_length = 64 - len(digest) - 1
    return f"{run_id[:prefix_length]}~{digest}"
