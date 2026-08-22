from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    CheckpointRecord,
    KnowledgeChunkRecord,
    ModelCallRecord,
    RunRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.harness.database_state_store import DatabaseStateStore
from mc_agent_harness.harness.execution_loop import ExecutionBudget, ExecutionLoop
from mc_agent_harness.harness.persistent_recorder import PersistentEvaluationRecorder
from mc_agent_harness.harness.tool_registry import ToolRegistry
from mc_agent_harness.knowledge.chunk_store import DatabaseKnowledgeStore, KnowledgeChunk
from mc_agent_harness.knowledge.database_provider import DatabaseKnowledgeProvider
from mc_agent_harness.knowledge.static_provider import StaticKnowledgeProvider
from mc_agent_harness.models.router import ModelCompletion, ModelProfile, ModelRouter
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction


class FakeRuntime(GameRuntime):
    """Runtime that returns deterministic observations and stores actions."""

    def __init__(self) -> None:
        self.actions: list[HarnessAction] = []

    async def reset(self, task_spec: dict[str, Any]) -> None:
        """Accept reset without side effects."""

        _ = task_spec

    async def observe(self) -> dict[str, Any]:
        """Return one stable observation for persistence tests."""

        return {"health": 20, "inventory": [], "nearby_blocks": [{"name": "oak_log"}]}

    async def act(self, action: HarnessAction) -> dict[str, Any]:
        """Record one action and return a successful result."""

        self.actions.append(action)
        return {"ok": True}

    async def snapshot(self) -> dict[str, Any]:
        """Return an empty snapshot."""

        return {"image": None}

    async def close(self) -> None:
        """Close the fake runtime."""


class FakeProvider:
    """Model provider that always returns a valid inventory action."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        profile: ModelProfile,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelCompletion:
        """Return one deterministic model completion."""

        _ = (messages, profile, response_schema)
        return ModelCompletion(content='{"type":"query_inventory","args":{}}')


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an in-memory SQLite session factory with Week 4 tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.anyio
async def test_persistent_recorder_writes_queryable_audit_tables(
    session_factory: sessionmaker[Session],
) -> None:
    recorder = PersistentEvaluationRecorder(session_factory)

    await recorder.record(
        "run_persist",
        "run_started",
        {"task_id": "inspect", "task_spec": {"task_id": "inspect"}},
    )
    await recorder.record(
        "run_persist",
        "observation",
        {"step_index": 0, "observation": {"health": 20}},
    )
    await recorder.record(
        "run_persist",
        "model_action",
        {
            "step_index": 0,
            "raw_content": '{"type":"query_inventory","args":{}}',
            "action": {"type": "query_inventory", "args": {}},
            "decision": {
                "reasoning_summary": "Inspect inventory before acting.",
                "evidence": ["inventory was not yet known"],
                "knowledge_need": {"needed": False, "query": None, "reason": None},
                "action": {"type": "query_inventory", "args": {}},
            },
            "usage": {"total_tokens": 10},
            "source": "model",
        },
    )
    await recorder.record(
        "run_persist",
        "action_result",
        {
            "step_index": 0,
            "action": {"type": "query_inventory", "args": {}},
            "result": {"ok": True},
        },
    )
    await recorder.record("run_persist", "run_finished", {"task_id": "inspect", "steps": 1})

    with session_factory() as session:
        run = session.get(RunRecord, "run_persist")
        assert run is not None
        assert run.status == "completed"
        assert (
            session.scalar(select(StepRecord).where(StepRecord.run_id == "run_persist")) is not None
        )
        model_call = session.scalar(
            select(ModelCallRecord).where(ModelCallRecord.run_id == "run_persist")
        )
        assert model_call is not None
        assert (
            model_call.raw_response["decision"]["reasoning_summary"]
            == "Inspect inventory before acting."
        )
        assert len(session.scalars(select(TrajectoryEventRecord)).all()) == 5


@pytest.mark.anyio
async def test_persistent_recorder_updates_terminal_task_statuses(
    session_factory: sessionmaker[Session],
) -> None:
    recorder = PersistentEvaluationRecorder(session_factory)

    await recorder.record("run_timeout", "run_started", {"task_id": "timeout_task"})
    await recorder.record("run_timeout", "run_task_timeout", {"task_id": "timeout_task"})
    await recorder.record("run_failed", "run_started", {"task_id": "failed_task"})
    await recorder.record(
        "run_failed",
        "verifier_result",
        {"task_id": "failed_task", "verifier": {"success": False, "reason": "missing target"}},
    )
    await recorder.record("run_succeeded", "run_started", {"task_id": "ok_task"})
    await recorder.record(
        "run_succeeded",
        "verifier_result",
        {"task_id": "ok_task", "verifier": {"success": True, "reason": "done"}},
    )
    await recorder.record("run_interrupted", "run_started", {"task_id": "cancelled_task"})
    await recorder.record(
        "run_interrupted",
        "run_interrupted",
        {"task_id": "cancelled_task", "reason": "keyboard_interrupt"},
    )
    await recorder.record("run_unverified", "run_started", {"task_id": "unverified_task"})
    await recorder.record(
        "run_unverified",
        "run_finished",
        {
            "task_id": "unverified_task",
            "terminated": True,
            "stop_reason": "agent_finished_unverified",
        },
    )

    with session_factory() as session:
        timeout_run = session.get(RunRecord, "run_timeout")
        failed_run = session.get(RunRecord, "run_failed")
        succeeded_run = session.get(RunRecord, "run_succeeded")
        interrupted_run = session.get(RunRecord, "run_interrupted")
        unverified_run = session.get(RunRecord, "run_unverified")
        assert timeout_run is not None
        assert failed_run is not None
        assert succeeded_run is not None
        assert interrupted_run is not None
        assert unverified_run is not None
        assert timeout_run.status == "task_timeout"
        assert timeout_run.finished_at is not None
        assert failed_run.status == "failed"
        assert failed_run.finished_at is not None
        assert succeeded_run.status == "succeeded"
        assert interrupted_run.status == "cancelled"
        assert unverified_run.status == "completed_unverified"
        assert unverified_run.finished_at is not None
        assert interrupted_run.finished_at is not None


@pytest.mark.anyio
async def test_persistent_recorder_publishes_event_after_commit(
    session_factory: sessionmaker[Session],
) -> None:
    """Event subscribers should only observe run_started after the row is queryable."""

    observed: list[tuple[str, str, str | None]] = []

    async def callback(run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        """Read the committed run from a separate session inside the callback."""

        _ = payload
        with session_factory() as session:
            run = session.get(RunRecord, run_id)
            observed.append((run_id, event_type, run.status if run is not None else None))

    recorder = PersistentEvaluationRecorder(session_factory, event_callback=callback)
    await recorder.record("run_callback", "run_started", {"task_id": "callback_task"})

    assert observed == [("run_callback", "run_started", "running")]


@pytest.mark.anyio
async def test_persistent_recorder_propagates_run_identity_to_every_event(
    session_factory: sessionmaker[Session],
) -> None:
    """Run-start identity should populate later event columns and payloads automatically."""

    recorder = PersistentEvaluationRecorder(session_factory)
    await recorder.record(
        "run_identity",
        "run_started",
        {
            "task_id": "identity_task",
            "task_spec": {
                "task_id": "identity_task",
                "runtime": {"username": "Trainer1"},
                "training": {"worker_id": "worker-1"},
            },
        },
    )
    await recorder.record(
        "run_identity",
        "observation",
        {"step_index": 0, "observation": {"health": 20}},
    )

    with session_factory() as session:
        events = session.scalars(
            select(TrajectoryEventRecord)
            .where(TrajectoryEventRecord.run_id == "run_identity")
            .order_by(TrajectoryEventRecord.id)
        ).all()

    assert len(events) == 2
    assert all(event.task_id == "identity_task" for event in events)
    assert all(event.agent_id == "Trainer1" for event in events)
    assert all(event.payload["agent_id"] == "Trainer1" for event in events)
    assert all(event.payload["worker_id"] == "worker-1" for event in events)


@pytest.mark.anyio
async def test_database_state_store_saves_and_loads_latest_checkpoint(
    session_factory: sessionmaker[Session],
) -> None:
    store = DatabaseStateStore(session_factory)

    await store.save_checkpoint(
        "run_checkpoint",
        {"task_id": "inspect", "step_index": 0, "next_step_index": 1, "task_memory": ["one"]},
    )
    await store.save_checkpoint(
        "run_checkpoint",
        {"task_id": "inspect", "step_index": 1, "next_step_index": 2, "task_memory": ["two"]},
    )

    checkpoint = await store.load_checkpoint("run_checkpoint")

    assert checkpoint is not None
    assert checkpoint["next_step_index"] == 2
    with session_factory() as session:
        assert len(session.scalars(select(CheckpointRecord)).all()) == 2


def test_database_knowledge_store_seeds_and_retrieves_chunks(
    session_factory: sessionmaker[Session],
) -> None:
    store = DatabaseKnowledgeStore(session_factory)

    count = store.upsert_static_provider(StaticKnowledgeProvider())
    docs = store.retrieve_docs("craft wooden pickaxe with oak planks", limit=3)

    assert count > 0
    assert docs
    assert any("wooden_pickaxe" in doc.id or "pickaxe" in doc.content for doc in docs)
    with session_factory() as session:
        assert len(session.scalars(select(KnowledgeChunkRecord)).all()) == count


def test_database_knowledge_store_filters_disabled_chunks_and_changes_revision(
    session_factory: sessionmaker[Session],
) -> None:
    store = DatabaseKnowledgeStore(session_factory)
    store.upsert_chunks(
        [
            KnowledgeChunk(
                id="editable-sheep",
                source="test",
                title="Sheep wool",
                content="A configured sheep wool fact.",
                tags=("sheep", "wool"),
            )
        ]
    )
    before = store.revision()
    assert [doc.id for doc in store.retrieve_docs("sheep wool")] == [
        "editable-sheep"
    ]

    with session_factory() as session:
        record = session.get(KnowledgeChunkRecord, "editable-sheep")
        assert record is not None
        record.enabled = False
        record.version += 1
        session.commit()

    after = store.revision()
    assert after != before
    assert store.retrieve_docs("sheep wool") == []


def test_database_knowledge_provider_does_not_resurface_archived_static_docs(
    session_factory: sessionmaker[Session],
) -> None:
    store = DatabaseKnowledgeStore(session_factory)
    store.upsert_static_provider(StaticKnowledgeProvider())
    with session_factory() as session:
        record = session.get(KnowledgeChunkRecord, "obtaining-feather")
        assert record is not None
        record.enabled = False
        record.version += 1
        session.commit()

    provider = DatabaseKnowledgeProvider(store)

    docs = provider.retrieve_docs("how to obtain feather harvest task chicken")
    assert all(document.id != "obtaining-feather" for document in docs)
    assert docs == store.retrieve_docs("how to obtain feather harvest task chicken")
    assert provider.resolve_terms("chicken")


@pytest.mark.anyio
async def test_execution_loop_persists_events_and_checkpoints(
    session_factory: sessionmaker[Session],
) -> None:
    recorder = PersistentEvaluationRecorder(session_factory)
    state_store = DatabaseStateStore(session_factory)
    loop = ExecutionLoop(
        runtime=FakeRuntime(),
        model_router=ModelRouter(provider=FakeProvider()),
        tool_registry=ToolRegistry(["query_inventory"]),
        recorder=recorder,
        state_store=state_store,
        budget=ExecutionBudget(max_steps=1, checkpoint_interval_steps=1),
    )

    result = await loop.run(
        "persisted_loop",
        task_spec={"run_id": "run_loop", "allowed_actions": ["query_inventory"]},
    )

    assert result.run_id == "run_loop"
    with session_factory() as session:
        assert session.get(RunRecord, "run_loop") is not None
        assert len(session.scalars(select(StepRecord)).all()) == 1
        assert len(session.scalars(select(CheckpointRecord)).all()) == 1
        event_types = [event.event_type for event in session.scalars(select(TrajectoryEventRecord))]
        assert "context_built" in event_types
        assert "checkpoint_saved" in event_types
