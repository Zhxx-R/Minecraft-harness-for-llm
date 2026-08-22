from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from mc_agent_harness.db.history_import import (
    HistoryImportStats,
    discover_audit_databases,
    import_historical_audits,
    select_best_run_sources,
)
from mc_agent_harness.db.models import (
    Base,
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
from mc_agent_harness.observability.tracing import (
    root_span_id_for_run,
    span_id_for_round,
    trace_id_for_run,
)


_HISTORICAL_TIME = datetime(2026, 7, 25, 12, 2, 40, tzinfo=UTC)


def _database(path: Path):
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_source(path: Path, *, rich: bool) -> None:
    engine = _database(path)
    with Session(engine) as session:
        session.add(
            RunRecord(
                id="run_duplicate",
                task_id="harvest_white_wool",
                status="running",
                task_spec={"goal": "collect white wool", "marker": "rich" if rich else "sparse"},
                started_at=_HISTORICAL_TIME,
                created_at=_HISTORICAL_TIME,
                updated_at=_HISTORICAL_TIME,
            )
        )
        session.flush()
        if rich:
            session.add_all(
                [
                    StepRecord(
                        id=901,
                        run_id="run_duplicate",
                        step_index=0,
                        observation={"nearby_entities": [{"entity_id": 68}]},
                        action={"type": "use_item", "args": {"entity_id": 68}},
                        action_result={"ok": True, "spawned_drops": [{"item": "brown_wool"}]},
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    TrajectoryEventRecord(
                        id=902,
                        run_id="run_duplicate",
                        event_type="action_result",
                        payload={"step_index": 0, "result": {"ok": True}},
                        task_id="harvest_white_wool",
                        agent_id="agent-1",
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    ModelCallRecord(
                        id=903,
                        run_id="run_duplicate",
                        step_index=0,
                        raw_content='{"type":"use_item"}',
                        action={"type": "use_item", "args": {"entity_id": 68}},
                        usage={"input_tokens": 100, "output_tokens": 20},
                        raw_response={"request_id": "request-1"},
                        source="model",
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    RuntimeErrorRecord(
                        id=904,
                        run_id="run_duplicate",
                        step_index=1,
                        error_type="recoverable",
                        message="target moved",
                        payload={"recoverable": True},
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    RoundSpanRecord(
                        id=910,
                        run_id="run_duplicate",
                        step_index=2,
                        status="active",
                        started_at=_HISTORICAL_TIME,
                        attributes={"source_only": True},
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    CreativeEvaluationRecord(
                        id=905,
                        run_id="run_duplicate",
                        task_id="harvest_white_wool",
                        status="completed",
                        prompt="white wool",
                        score=0.75,
                        score_threshold=0.5,
                        success=True,
                        scorer="mineclip",
                        variant="vit-b-16",
                        calibration_status="human_in_the_loop",
                        frame_count=3,
                        window_count=1,
                        result={"key_frames": ["frame-1.png"]},
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    HumanReviewRecord(
                        id=906,
                        run_id="run_duplicate",
                        task_id="harvest_white_wool",
                        task_name="Collect white wool",
                        status="approved",
                        submission_summary="Goal visible",
                        evidence={"video": "demo.mp4"},
                        reviewer_id="reviewer-1",
                        decision="approved",
                        reason_codes=["goal_satisfied"],
                        notes="Looks correct",
                        submitted_at=_HISTORICAL_TIME,
                        decided_at=_HISTORICAL_TIME,
                        version=2,
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    SkillRecord(
                        id=907,
                        name="harvest_wool",
                        version="1",
                        status="active",
                        spec={"steps": ["find", "follow", "shear"]},
                        source_run_id="run_duplicate",
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    LearningCandidateRecord(
                        id=908,
                        signature="sheep-target-moved",
                        scope_key="harvest",
                        kind="recovery",
                        status="validated",
                        hypothesis="Follow before using shears",
                        failure_status="recoverable",
                        action_type="use_item",
                        target="sheep",
                        support_count=2,
                        recovery_count=1,
                        contradiction_count=0,
                        confidence=0.8,
                        evidence={"step": 1},
                        knowledge_refs=[{"id": "entity-follow"}],
                        source_run_ids=["run_duplicate"],
                        recovery_run_ids=["run_duplicate"],
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                    TaskMemoryRecord(
                        id=909,
                        task_id="harvest_white_wool",
                        namespace="entities",
                        content="entity 68 produced brown wool",
                        memory_metadata={"entity_id": 68},
                        created_at=_HISTORICAL_TIME,
                        updated_at=_HISTORICAL_TIME,
                    ),
                ]
            )
        else:
            session.add(
                TrajectoryEventRecord(
                    id=801,
                    run_id="run_duplicate",
                    event_type="run_started",
                    payload={"task_id": "harvest_white_wool"},
                    task_id="harvest_white_wool",
                    created_at=_HISTORICAL_TIME,
                    updated_at=_HISTORICAL_TIME,
                )
            )
        session.commit()
    engine.dispose()


def _row_count(session: Session, model: type[Base]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_selects_most_complete_source_and_imports_all_supported_records(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    sparse_path = runs_root / "a" / "audit.sqlite3"
    rich_path = runs_root / "z" / "audit.sqlite3"
    sparse_path.parent.mkdir(parents=True)
    rich_path.parent.mkdir(parents=True)
    _seed_source(sparse_path, rich=False)
    _seed_source(rich_path, rich=True)

    discovered = discover_audit_databases(runs_root)
    selection_stats = HistoryImportStats(dry_run=True, runs_root=str(runs_root))
    selected = select_best_run_sources(discovered, runs_root, selection_stats)
    assert selected["run_duplicate"].relative_source == "z/audit.sqlite3"
    assert selection_stats.duplicate_run_sources == 1

    target_path = tmp_path / "target.sqlite3"
    target_engine = _database(target_path)
    stats = import_historical_audits(
        runs_root=runs_root,
        engine=target_engine,
    )

    assert stats.scanned_databases == 2
    assert stats.unique_runs == 1
    assert stats.runs_normalized_from_running == 1
    assert stats.rows_imported == {
        "creative_evaluations": 1,
        "human_reviews": 1,
        "learning_candidates": 1,
        "model_calls": 1,
        "round_spans": 3,
        "runs": 1,
        "runtime_errors": 1,
        "skills": 1,
        "steps": 1,
        "task_memories": 1,
        "trajectory_events": 1,
    }

    with Session(target_engine) as session:
        run = session.get(RunRecord, "run_duplicate")
        assert run is not None
        assert run.status == "interrupted"
        assert run.task_spec["goal"] == "collect white wool"
        assert run.task_spec["_historical_import"] == {
            "source_database": "z/audit.sqlite3",
            "source_table": "runs",
            "source_row_id": "run_duplicate",
            "original_status": "running",
            "original_resumed_from_checkpoint_id": None,
        }
        assert run.started_at.date() == _HISTORICAL_TIME.date()
        assert run.trace_id == trace_id_for_run(run.id)
        assert run.root_span_id == root_span_id_for_run(run.id)

        step = session.scalar(select(StepRecord))
        assert step is not None
        assert step.id != 901
        assert step.action_result["spawned_drops"] == [{"item": "brown_wool"}]
        assert (
            step.action_result["_historical_import"]["source_database"]
            == "z/audit.sqlite3"
        )
        assert step.trace_id == run.trace_id
        assert step.span_id == span_id_for_round(run.id, 0)

        model_call = session.scalar(select(ModelCallRecord))
        assert model_call is not None
        assert model_call.id != 903
        assert model_call.usage == {"input_tokens": 100, "output_tokens": 20}
        assert model_call.raw_response["request_id"] == "request-1"
        assert model_call.trace_id == run.trace_id
        assert model_call.span_id == step.span_id

        event = session.scalar(select(TrajectoryEventRecord))
        assert event is not None
        assert event.step_index == 0
        assert event.trace_id == run.trace_id
        assert event.span_id == step.span_id
        assert event.payload["trace_id"] == run.trace_id
        assert event.payload["span_id"] == step.span_id

        runtime_error = session.scalar(select(RuntimeErrorRecord))
        assert runtime_error is not None
        assert runtime_error.step_index == 1
        assert runtime_error.trace_id == run.trace_id
        assert runtime_error.span_id == span_id_for_round(run.id, 1)
        assert runtime_error.payload["trace_id"] == run.trace_id
        assert runtime_error.payload["span_id"] == runtime_error.span_id
        assert runtime_error.payload["step_index"] == 1

        round_spans = {
            span.step_index: span
            for span in session.scalars(
                select(RoundSpanRecord).order_by(RoundSpanRecord.step_index)
            ).all()
        }
        assert set(round_spans) == {0, 1, 2}
        assert round_spans[0].trace_id == run.trace_id
        assert round_spans[0].span_id == step.span_id
        assert round_spans[0].parent_span_id == run.root_span_id
        assert round_spans[0].status == "ok"
        assert round_spans[0].attributes["has_step_record"] is True
        assert round_spans[1].span_id == runtime_error.span_id
        assert round_spans[1].parent_span_id == run.root_span_id
        assert round_spans[1].status == "error"
        assert round_spans[1].attributes["has_step_record"] is False
        assert round_spans[1].attributes["runtime_error_count"] == 1
        assert round_spans[2].status == "incomplete"
        assert round_spans[2].attributes["source_span_present"] is True
        assert round_spans[2].attributes["has_step_record"] is False

        skill = session.scalar(select(SkillRecord))
        assert skill is not None
        assert skill.id != 907
        assert skill.source_run_id == "run_duplicate"
        assert (
            skill.spec["_historical_import"]["original_source_run_id"]
            == "run_duplicate"
        )

        memory = session.scalar(select(TaskMemoryRecord))
        assert memory is not None
        assert memory.id != 909
        assert memory.memory_metadata["entity_id"] == 68

        assert _row_count(session, CreativeEvaluationRecord) == 1
        assert _row_count(session, HumanReviewRecord) == 1
        assert _row_count(session, LearningCandidateRecord) == 1
        assert _row_count(session, RuntimeErrorRecord) == 1
        assert _row_count(session, TrajectoryEventRecord) == 1
        assert _row_count(session, RoundSpanRecord) == 3

    second = import_historical_audits(
        runs_root=runs_root,
        engine=target_engine,
    )
    assert second.existing_runs_skipped == 1
    assert second.rows_imported == {}
    with Session(target_engine) as session:
        assert _row_count(session, RunRecord) == 1
        assert _row_count(session, RoundSpanRecord) == 3
        assert _row_count(session, StepRecord) == 1
        assert _row_count(session, SkillRecord) == 1
        assert _row_count(session, TaskMemoryRecord) == 1


def test_dry_run_rolls_back_every_imported_record(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    source_path = runs_root / "nested" / "audit.sqlite3"
    source_path.parent.mkdir(parents=True)
    _seed_source(source_path, rich=True)
    target_engine = _database(tmp_path / "target.sqlite3")

    stats = import_historical_audits(
        runs_root=runs_root,
        engine=target_engine,
        dry_run=True,
    )

    assert stats.dry_run is True
    assert stats.rows_imported["runs"] == 1
    assert stats.rows_imported["round_spans"] == 3
    assert stats.rows_imported["steps"] == 1
    with Session(target_engine) as session:
        assert _row_count(session, RunRecord) == 0
        assert _row_count(session, RoundSpanRecord) == 0
        assert _row_count(session, StepRecord) == 0
        assert _row_count(session, SkillRecord) == 0


def test_long_historical_run_ids_are_mapped_and_keep_source_identity(
    tmp_path: Path,
) -> None:
    runs_root = tmp_path / "runs"
    source_path = runs_root / "long-id" / "audit.sqlite3"
    source_path.parent.mkdir(parents=True)
    source_engine = _database(source_path)
    source_run_id = (
        "week10_live_worker-1_harvest_1_glass_swampland_with_furnace_and_fuel_"
        "42c44fb6"
    )
    with Session(source_engine) as session:
        session.add(
            RunRecord(
                id=source_run_id,
                task_id="harvest_1_glass_swampland_with_furnace_and_fuel",
                status="succeeded",
                task_spec={"goal": "collect glass"},
                started_at=_HISTORICAL_TIME,
                finished_at=_HISTORICAL_TIME,
                created_at=_HISTORICAL_TIME,
                updated_at=_HISTORICAL_TIME,
            )
        )
        session.add(
            StepRecord(
                run_id=source_run_id,
                step_index=0,
                observation={},
                action={"type": "observe"},
                action_result={"ok": True},
                created_at=_HISTORICAL_TIME,
                updated_at=_HISTORICAL_TIME,
            )
        )
        session.add(
            SkillRecord(
                name="harvest_glass",
                version="1",
                status="active",
                spec={"steps": ["collect"]},
                source_run_id=source_run_id,
                created_at=_HISTORICAL_TIME,
                updated_at=_HISTORICAL_TIME,
            )
        )
        session.commit()
    source_engine.dispose()

    target_engine = _database(tmp_path / "target.sqlite3")
    import_historical_audits(runs_root=runs_root, engine=target_engine)

    with Session(target_engine) as session:
        imported_run = session.scalar(select(RunRecord))
        assert imported_run is not None
        assert len(imported_run.id) == 64
        assert imported_run.id != source_run_id
        assert imported_run.task_spec["_historical_import"]["original_run_id"] == source_run_id
        assert imported_run.trace_id == trace_id_for_run(imported_run.id)
        assert imported_run.root_span_id == root_span_id_for_run(imported_run.id)
        assert imported_run.trace_id != trace_id_for_run(source_run_id)

        imported_step = session.scalar(select(StepRecord))
        assert imported_step is not None
        assert imported_step.run_id == imported_run.id
        assert imported_step.trace_id == imported_run.trace_id
        assert imported_step.span_id == span_id_for_round(imported_run.id, 0)

        imported_span = session.scalar(select(RoundSpanRecord))
        assert imported_span is not None
        assert imported_span.run_id == imported_run.id
        assert imported_span.trace_id == imported_run.trace_id
        assert imported_span.span_id == imported_step.span_id
        assert imported_span.parent_span_id == imported_run.root_span_id

        imported_skill = session.scalar(select(SkillRecord))
        assert imported_skill is not None
        assert imported_skill.source_run_id == imported_run.id
