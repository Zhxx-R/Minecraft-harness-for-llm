from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    LearningCandidateRecord,
    RunRecord,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.harness.context_manager import ContextManager
from mc_agent_harness.schemas.learning import LearningCandidateKind, LearningCandidateStatus
from mc_agent_harness.skills.learning import FailureClassifier, LearningCandidateStore
from mc_agent_harness.skills.library import SkillLibrary


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """Create an isolated persistence store for failure-learning tests."""

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'learning.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.anyio
async def test_infrastructure_failure_is_audited_but_not_learned(
    session_factory: sessionmaker[Session],
) -> None:
    """Model and runtime timeouts must never become gameplay learning candidates."""

    with session_factory() as session:
        session.add(_run("timeout-run", status="model_timeout"))
        session.add(
            StepRecord(
                run_id="timeout-run",
                step_index=0,
                observation={},
                action={"type": "move_to", "args": {"position": {"x": 8, "y": 64, "z": 0}}},
                action_result={"ok": False, "error_code": "no_path"},
            )
        )
        session.commit()

    decision = await LearningCandidateStore(session_factory).record_failure("timeout-run")

    assert decision.should_record is False
    assert decision.reason == "excluded_run_status:model_timeout"
    with session_factory() as session:
        assert session.scalar(select(LearningCandidateRecord)) is None
        event = session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.event_type == "learning_candidate_skipped"
            )
        )
        assert event is not None


def test_single_navigation_timeout_is_not_durable_learning_evidence() -> None:
    """One static stall can be transient and must not create a failure hypothesis."""

    decision = FailureClassifier().classify(
        _run("single-stall", status="task_timeout"),
        [_stalled_move(0, x=8)],
        [],
    )

    assert decision.should_record is False
    assert decision.reason == "excluded_run_status:task_timeout"


def test_repeated_static_navigation_timeout_is_durable_learning_evidence() -> None:
    """Two diagnosed no-progress attempts at one target can support navigation learning."""

    decision = FailureClassifier().classify(
        _run("repeated-stall", status="task_timeout"),
        [_stalled_move(0, x=8), _stalled_move(2, x=8.5)],
        [],
    )

    assert decision.should_record is True
    assert decision.candidate is not None
    assert decision.candidate.kind == LearningCandidateKind.navigation_recovery
    assert decision.candidate.failure_status == "timeout_no_progress"


def test_moving_target_navigation_timeouts_are_not_merged() -> None:
    """No-progress results at distant coordinates must not be treated as one static blocker."""

    decision = FailureClassifier().classify(
        _run("moving-target", status="task_timeout"),
        [_stalled_move(0, x=8), _stalled_move(2, x=16)],
        [],
    )

    assert decision.should_record is False


def test_task_timeout_does_not_learn_unrelated_durable_failure() -> None:
    """A task budget exit must not promote an earlier unrelated combat outcome."""

    decision = FailureClassifier().classify(
        _run("timed-out-combat", status="task_timeout"),
        [
            StepRecord(
                run_id="timed-out-combat",
                step_index=0,
                observation={},
                action={
                    "type": "move_to_and_engage_combat",
                    "args": {"entity": "enderman", "mode": "melee"},
                },
                action_result={"ok": False, "status": "target_lost"},
            )
        ],
        [],
    )

    assert decision.should_record is False
    assert decision.reason == "excluded_run_status:task_timeout"


@pytest.mark.anyio
async def test_repeated_failure_is_deduplicated_and_corroborated(
    session_factory: sessionmaker[Session],
) -> None:
    """Repeated same-scope failures increase support instead of creating duplicate skills."""

    with session_factory() as session:
        for run_id in ("failure-one", "failure-two"):
            session.add(_run(run_id, status="failed"))
            session.add(
                StepRecord(
                    run_id=run_id,
                    step_index=0,
                    observation={"nearby_entities": [{"name": "enderman"}]},
                    action={"type": "engage_combat", "args": {"entity": "enderman", "mode": "melee"}},
                    action_result={"ok": False, "status": "target_lost"},
                )
            )
        session.commit()

    store = LearningCandidateStore(session_factory)
    first = await store.record_failure("failure-one")
    second = await store.record_failure("failure-two")

    assert first.candidate is not None
    assert first.candidate.status == LearningCandidateStatus.observed
    assert second.candidate is not None
    assert second.candidate.status == LearningCandidateStatus.corroborated
    assert second.candidate.support_count == 2
    with session_factory() as session:
        assert len(session.scalars(select(LearningCandidateRecord)).all()) == 1
        assert session.scalar(select(SkillRecord)) is None


@pytest.mark.anyio
async def test_knowledge_backed_failure_requires_success_before_skill_creation(
    session_factory: sessionmaker[Session],
) -> None:
    """Knowledge creates a hypothesis; verifier-backed recovery validates and enriches a skill."""

    with session_factory() as session:
        session.add(_run("failed-run", status="failed"))
        session.add(
            StepRecord(
                run_id="failed-run",
                step_index=0,
                observation={"nearby_entities": [{"name": "enderman"}]},
                action={"type": "engage_combat", "args": {"entity": "enderman", "mode": "melee"}},
                action_result={"ok": False, "status": "target_lost"},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="failed-run",
                event_type="knowledge_tool_call",
                task_id="combat_enderman",
                agent_id="Trainer1",
                payload={
                    "step_index": 1,
                    "action": {"type": "retrieve_docs", "args": {"query": "enderman behavior"}},
                    "result": {
                        "ok": True,
                        "tool": "retrieve_docs",
                        "query": "enderman behavior",
                        "docs": [{"id": "entity/enderman", "title": "Enderman"}],
                    },
                },
            )
        )
        session.commit()

    store = LearningCandidateStore(session_factory)
    failed = await store.record_failure("failed-run")

    assert failed.candidate is not None
    assert failed.candidate.status == LearningCandidateStatus.hypothesized
    with session_factory() as session:
        assert session.scalar(select(SkillRecord)) is None
        session.add(_run("recovery-run", status="succeeded"))
        session.add(
            StepRecord(
                run_id="recovery-run",
                step_index=0,
                observation={"nearby_entities": [{"name": "enderman"}]},
                action={"type": "engage_combat", "args": {"entity": "enderman", "mode": "ranged"}},
                action_result={"ok": True, "status": "target_killed", "entity": "enderman"},
            )
        )
        session.commit()

    validated = await store.record_success("recovery-run")

    assert len(validated) == 1
    assert validated[0].status == LearningCandidateStatus.validated
    assert validated[0].recovery_run_ids == ["recovery-run"]
    assert "validated recovery actions [engage_combat]" in validated[0].hypothesis

    skill = await SkillLibrary(session_factory=session_factory).create_candidate(
        "recovery-run",
        learning_candidates=validated,
    )

    assert skill.name == "defeat_enderman"
    assert skill.validation["failure_learning_gate"] == "validated_by_successful_verifier"
    assert skill.source_evidence["learning_candidates"][0]["signature"] == validated[0].signature
    assert "target_lost" in skill.triggers


@pytest.mark.anyio
async def test_learning_snapshot_is_exact_scope_and_marks_hypotheses(
    session_factory: sessionmaker[Session],
) -> None:
    """The next batch sees only matching active hypotheses with non-authoritative semantics."""

    with session_factory() as session:
        session.add(_run("failed-run", status="failed"))
        session.add(
            StepRecord(
                run_id="failed-run",
                step_index=0,
                observation={},
                action={"type": "engage_combat", "args": {"entity": "enderman"}},
                action_result={"ok": False, "status": "target_unreachable"},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="failed-run",
                event_type="knowledge_tool_call",
                task_id="combat_enderman",
                agent_id="Trainer1",
                payload={
                    "result": {
                        "ok": True,
                        "tool": "retrieve_docs",
                        "query": "enderman reachability",
                        "docs": [{"id": "entity/enderman", "title": "Enderman"}],
                    }
                },
            )
        )
        session.commit()

    store = LearningCandidateStore(session_factory)
    await store.record_failure("failed-run")
    snapshot = await store.capture_snapshot()
    manager = ContextManager(learning_candidates=snapshot)

    matching = await manager.build(
        observation={"inventory": [], "nearby_entities": []},
        task_memory=[],
        task_spec=_task_spec(),
        allowed_actions=["retrieve_docs", "scan_entities", "engage_combat"],
    )
    unrelated = await manager.build(
        observation={"inventory": [], "nearby_blocks": []},
        task_memory=[],
        task_spec={
            "task_id": "harvest_oak_log",
            "category": "harvest",
            "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
        },
        allowed_actions=["scan_blocks", "dig_block_at"],
    )

    assert len(matching.retrieved_learning_candidates) == 1
    payload = matching.prompt_sections["user_payload"]
    assert payload["retrieved_learning_candidates"][0]["semantics"] == (
        "scoped_hypothesis_not_authoritative_instruction"
    )
    assert unrelated.retrieved_learning_candidates == []


def _run(run_id: str, *, status: str) -> RunRecord:
    """Build one combat run with stable task and agent identity metadata."""

    return RunRecord(
        id=run_id,
        task_id="combat_enderman",
        status=status,
        task_spec=_task_spec(),
    )


def _task_spec() -> dict[str, object]:
    """Return a quantity-independent combat task used across tests."""

    return {
        "task_id": "combat_enderman",
        "category": "combat",
        "goal": "Defeat one enderman.",
        "verifier": {"type": "entity_defeated", "entity": "enderman"},
        "runtime": {"username": "Trainer1"},
        "training": {"worker_id": "worker-1", "agent_id": "Trainer1"},
    }


def _stalled_move(step_index: int, *, x: float) -> StepRecord:
    """Build one fully diagnosed pathfinder timeout with no measurable distance progress."""

    return StepRecord(
        run_id="timeout-run",
        step_index=step_index,
        observation={},
        action={"type": "move_to", "args": {"position": {"x": x, "y": 64, "z": 0}}},
        action_result={
            "ok": False,
            "status": "timeout_no_progress",
            "progress_status": "timeout_no_progress",
            "initial_distance": 10.0,
            "final_distance": 9.8,
            "timeout_ms": 12000,
            "path_summary": {"status": "partial", "visited_nodes": 50},
            "nearest_reachable_position": {"x": x - 1, "y": 64, "z": 0},
        },
    )
