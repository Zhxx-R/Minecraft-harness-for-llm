import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    ModelCallRecord,
    RoundSpanRecord,
    RunRecord,
    RuntimeErrorRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.harness.evaluation import EvaluationRecorder
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder
from mc_agent_harness.observability.tracing import (
    root_span_id_for_run,
    span_id_for_round,
    trace_id_for_run,
)


TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """Create an isolated SQLite database containing the complete audit schema."""

    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'tracing.sqlite3'}",
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_trace_identifiers_are_stable_distinct_and_w3c_compatible() -> None:
    """A run and each of its rounds should receive stable non-zero hex identifiers."""

    run_id = "run_trace_identity"
    trace_id = trace_id_for_run(run_id)
    root_span_id = root_span_id_for_run(run_id)
    round_zero_span_id = span_id_for_round(run_id, 0)
    round_one_span_id = span_id_for_round(run_id, 1)

    assert TRACE_ID_PATTERN.fullmatch(trace_id)
    assert SPAN_ID_PATTERN.fullmatch(root_span_id)
    assert SPAN_ID_PATTERN.fullmatch(round_zero_span_id)
    assert SPAN_ID_PATTERN.fullmatch(round_one_span_id)
    assert int(trace_id, 16) != 0
    assert all(
        int(span_id, 16) != 0
        for span_id in (root_span_id, round_zero_span_id, round_one_span_id)
    )

    assert trace_id_for_run(run_id) == trace_id
    assert root_span_id_for_run(run_id) == root_span_id
    assert span_id_for_round(run_id, 0) == round_zero_span_id
    assert trace_id_for_run("another_run") != trace_id
    assert root_span_id_for_run("another_run") != root_span_id
    assert round_zero_span_id != round_one_span_id
    assert root_span_id not in {round_zero_span_id, round_one_span_id}


@pytest.mark.anyio
async def test_evaluation_recorder_maps_run_events_to_root_and_round_events_to_child_span() -> None:
    """In-memory logs should carry the same root-versus-round trace context as SQL logs."""

    run_id = "run_in_memory_trace"
    recorder = EvaluationRecorder()
    run_payload = {"task_id": "trace_task"}
    round_payload = {"step_index": 3, "observation": {"health": 20}}

    await recorder.record(run_id, "run_started", run_payload)
    await recorder.record(run_id, "observation", round_payload)

    root_event, round_event = recorder.events
    expected_trace_id = trace_id_for_run(run_id)

    assert root_event.payload["trace_id"] == expected_trace_id
    assert root_event.payload["span_id"] == root_span_id_for_run(run_id)
    assert round_event.payload["trace_id"] == expected_trace_id
    assert round_event.payload["span_id"] == span_id_for_round(run_id, 3)
    assert run_payload == {"task_id": "trace_task"}
    assert round_payload == {"step_index": 3, "observation": {"health": 20}}


@pytest.mark.anyio
async def test_persistent_recorder_keeps_trace_context_consistent_across_audit_tables(
    session_factory: sessionmaker[Session],
) -> None:
    """All records for a round should share its span, including a round with no StepRecord."""

    run_id = "run_persistent_trace"
    recorder = PersistentEvaluationRecorder(session_factory)

    await recorder.record(
        run_id,
        "run_started",
        {"task_id": "trace_task", "task_spec": {"task_id": "trace_task"}},
    )
    await recorder.record(
        run_id,
        "observation",
        {"step_index": 0, "observation": {"health": 20}},
    )
    await recorder.record(
        run_id,
        "model_action",
        {
            "step_index": 0,
            "raw_content": '{"type":"query_inventory","args":{}}',
            "action": {"type": "query_inventory", "args": {}},
            "usage": {"total_tokens": 12},
        },
    )
    await recorder.record(
        run_id,
        "action_result",
        {
            "step_index": 0,
            "action": {"type": "query_inventory", "args": {}},
            "result": {"ok": True},
        },
    )
    await recorder.record(
        run_id,
        "observation",
        {"step_index": 1, "observation": {"health": 18}},
    )
    await recorder.record(
        run_id,
        "runtime_error",
        {
            "step_index": 1,
            "error_type": "worker_error",
            "message": "worker disconnected before action_result",
        },
    )
    await recorder.record(run_id, "run_finished", {"task_id": "trace_task", "steps": 2})

    expected_trace_id = trace_id_for_run(run_id)
    expected_root_span_id = root_span_id_for_run(run_id)
    expected_round_spans = {
        0: span_id_for_round(run_id, 0),
        1: span_id_for_round(run_id, 1),
    }

    with session_factory() as session:
        run = session.get(RunRecord, run_id)
        round_spans = session.scalars(
            select(RoundSpanRecord)
            .where(RoundSpanRecord.run_id == run_id)
            .order_by(RoundSpanRecord.step_index)
        ).all()
        events = session.scalars(
            select(TrajectoryEventRecord)
            .where(TrajectoryEventRecord.run_id == run_id)
            .order_by(TrajectoryEventRecord.id)
        ).all()
        steps = session.scalars(
            select(StepRecord).where(StepRecord.run_id == run_id)
        ).all()
        model_calls = session.scalars(
            select(ModelCallRecord).where(ModelCallRecord.run_id == run_id)
        ).all()
        runtime_errors = session.scalars(
            select(RuntimeErrorRecord).where(RuntimeErrorRecord.run_id == run_id)
        ).all()

    assert run is not None
    assert run.trace_id == expected_trace_id
    assert run.root_span_id == expected_root_span_id

    assert [span.step_index for span in round_spans] == [0, 1]
    assert all(span.trace_id == expected_trace_id for span in round_spans)
    assert all(span.parent_span_id == expected_root_span_id for span in round_spans)
    assert {
        span.step_index: span.span_id for span in round_spans
    } == expected_round_spans
    assert {span.step_index: span.status for span in round_spans} == {0: "ok", 1: "error"}

    assert len(steps) == 1
    assert steps[0].step_index == 0
    assert steps[0].trace_id == expected_trace_id
    assert steps[0].span_id == expected_round_spans[0]

    assert len(model_calls) == 1
    assert model_calls[0].step_index == 0
    assert model_calls[0].trace_id == expected_trace_id
    assert model_calls[0].span_id == expected_round_spans[0]

    assert len(runtime_errors) == 1
    assert runtime_errors[0].step_index == 1
    assert runtime_errors[0].trace_id == expected_trace_id
    assert runtime_errors[0].span_id == expected_round_spans[1]
    assert runtime_errors[0].payload["trace_id"] == expected_trace_id
    assert runtime_errors[0].payload["span_id"] == expected_round_spans[1]

    assert len(events) == 7
    for event in events:
        expected_span_id = (
            expected_root_span_id
            if event.step_index is None
            else expected_round_spans[event.step_index]
        )
        assert event.trace_id == expected_trace_id
        assert event.span_id == expected_span_id
        assert event.payload["trace_id"] == expected_trace_id
        assert event.payload["span_id"] == expected_span_id

    assert not any(step.step_index == 1 for step in steps)
    assert any(span.step_index == 1 for span in round_spans)
