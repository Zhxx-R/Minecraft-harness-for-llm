import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mc_agent_harness.db.models import (
    Base,
    LearningCandidateRecord,
    RunRecord,
    RuntimeErrorRecord,
    SkillRecord,
    StepRecord,
    TaskMemoryRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.evaluation.benchmark import ScriptedActionProvider, ScriptedBenchmarkRuntime
from mc_agent_harness.models.router import ModelCompletion, ModelProfile, ModelRouter, ModelUsage
from mc_agent_harness.runtime.game_runtime import GameRuntime
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.library import SkillLibrary
from mc_agent_harness.tasks.minedojo_provider import MineDojoTaskProvider
from mc_agent_harness.training.runner import TrainingTaskRequest
from mc_agent_harness.training import (
    LiveMinecraftConfig,
    LiveTrainingOutcome,
    LiveTrainingConfig,
    LiveTrainingRunner,
    LiveTrainingResumeState,
    LiveWorkerSpec,
    RandomTeleportResetConfig,
    TrainingBudget,
)
from mc_agent_harness.training.live_runner import _run_id


ROOT = Path(__file__).resolve().parents[3]
MANIFEST_DIR = ROOT / "tasks" / "manifests"


@pytest.fixture
def anyio_backend() -> str:
    """Force AnyIO tests in this module to use asyncio."""

    return "asyncio"


def test_live_training_run_id_fits_database_primary_key() -> None:
    """Keep formal task-attempt ids within the runs.id VARCHAR(64) contract."""

    first = _run_id(
        "week10_live_20260718T082317Z",
        "combat_bat_extreme_hills_barehand",
        "worker-1",
        1,
    )
    second = _run_id(
        "week10_live_20260718T082317Z",
        "combat_bat_extreme_hills_barehand",
        "worker-1",
        1,
    )

    assert len(first) <= 64
    assert len(second) <= 64
    assert first != second


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    """Create a file-backed SQLite session factory for live-training tests."""

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'live_training.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.anyio
async def test_week10_live_training_promotes_successful_skill(
    session_factory: sessionmaker[Session],
) -> None:
    """Validate live training success creates and promotes a skill candidate."""

    runner = _runner(session_factory, auto_promote=True)

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "succeeded"
    assert report.success_count == 1
    assert report.outcomes[0].skill_update is not None
    assert report.outcomes[0].skill_update.promoted is not None
    with session_factory() as session:
        skill = session.scalar(select(SkillRecord).where(SkillRecord.name == "harvest_oak_log"))
        assert skill is not None
        assert skill.status == SkillStatus.promoted.value


@pytest.mark.anyio
async def test_week10_live_training_dedup_skips_duplicate_promotion(
    session_factory: sessionmaker[Session],
) -> None:
    """Validate duplicate skill candidates are detected before promotion."""

    existing = SkillSpec(
        name="harvest_oak_log",
        version="0.1.0",
        description="Collect one nearby oak log.",
        triggers=["oak_log", "wood", "harvest"],
        action_plan=[
            HarnessAction(type="scan_blocks", args={"block": "oak_log", "count": 4}),
            HarnessAction(
                type="move_to", args={"position": {"x": 1, "y": 65, "z": 0}, "tolerance": 1.5}
            ),
            HarnessAction(
                type="dig_block_at",
                args={"position": {"x": 1, "y": 65, "z": 0}, "block": "oak_log"},
            ),
            HarnessAction(type="scan_dropped_items", args={"item": "oak_log", "max_distance": 6}),
            HarnessAction(
                type="move_to", args={"position": {"x": 1, "y": 65, "z": 0}, "tolerance": 1.0}
            ),
            HarnessAction(type="wait_ticks", args={"ticks": 10}),
        ],
        task_scope=[
            "minecraft:harvest",
            "action:scan_blocks",
            "action:scan_dropped_items",
            "action:move_to",
            "action:dig_block_at",
            "action:wait_ticks",
        ],
        dependencies=[
            "oak_log",
            "action:scan_blocks",
            "action:scan_dropped_items",
            "action:move_to",
            "action:dig_block_at",
            "action:wait_ticks",
        ],
        status=SkillStatus.promoted,
    )
    with session_factory() as session:
        session.add(
            SkillRecord(
                name=existing.name,
                version=existing.version,
                status=existing.status.value,
                spec=existing.model_dump(mode="json"),
            )
        )
        session.commit()
    runner = _runner(session_factory, auto_promote=True)

    report = await runner.run(["minedojo_harvest_oak_log"])

    update = report.outcomes[0].skill_update
    assert update is not None
    assert update.promoted is None
    assert update.skipped_reason == "duplicate_candidate"
    assert update.duplicate_matches
    with session_factory() as session:
        records = session.scalars(
            select(SkillRecord).where(SkillRecord.name == "harvest_oak_log")
        ).all()
        assert sorted(record.status for record in records) == [
            SkillStatus.draft.value,
            SkillStatus.promoted.value,
        ]


@pytest.mark.anyio
async def test_week10_live_training_defers_skill_writes_and_freezes_batch_snapshot(
    session_factory: sessionmaker[Session],
) -> None:
    """Parallel runs should share one immutable skill view and write skills after the barrier."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            ),
            LiveWorkerSpec(
                worker_id="worker-2",
                worker_url="fake://worker-2",
                username="Trainer2",
            ),
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_batch_skill_barrier_test",
            auto_promote=True,
            budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=2),
        ),
        runtime_factory=lambda _url, _timeout: ScriptedBenchmarkRuntime(),
        model_router_factory=_scripted_router,
    )

    report = await runner.run(["minedojo_harvest_oak_log", "minedojo_harvest_dirt"])

    assert report.status == "succeeded"
    assert report.success_count == 2
    assert report.skill_snapshot is not None
    assert report.skill_snapshot["skill_count"] == 2
    with session_factory() as session:
        runs = session.scalars(select(RunRecord).order_by(RunRecord.task_id)).all()
        events = session.scalars(
            select(TrajectoryEventRecord).order_by(TrajectoryEventRecord.id)
        ).all()
        generated_skills = session.scalars(
            select(SkillRecord).where(SkillRecord.source_run_id.is_not(None))
        ).all()

    snapshots = [run.task_spec["training"]["skill_snapshot"] for run in runs]
    assert len({snapshot["revision"] for snapshot in snapshots}) == 1
    assert all(snapshot["skill_count"] == 2 for snapshot in snapshots)
    verifier_event_ids = [event.id for event in events if event.event_type == "verifier_result"]
    finalize_event_ids = [
        event.id for event in events if event.event_type == "skill_batch_finalize_started"
    ]
    assert len(verifier_event_ids) == 2
    assert len(finalize_event_ids) == 2
    assert min(finalize_event_ids) > max(verifier_event_ids)

    generated_names = {record.name for record in generated_skills}
    retrieved_names = {
        str(skill["name"])
        for event in events
        if event.event_type == "context_built"
        for skill in event.payload.get("retrieved_skills", [])
    }
    assert generated_names
    assert generated_names.isdisjoint(retrieved_names)
    assert all(event.agent_id is not None for event in events)
    assert all(event.payload.get("worker_id") is not None for event in events)


@pytest.mark.anyio
async def test_week10_live_training_requires_inventory_delta(
    session_factory: sessionmaker[Session],
) -> None:
    """Pre-existing target items should not make live training succeed."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_live_delta_test",
            auto_promote=True,
            budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: PreloadedInventoryRuntime(),
        model_router_factory=lambda _task_spec: _fixed_router(
            {"type": "query_inventory", "args": {}}
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "completed_with_failures"
    assert report.success_count == 0
    assert report.outcomes[0].success is False
    assert report.outcomes[0].skill_update is None
    assert report.outcomes[0].learning_update is not None
    assert report.outcomes[0].learning_update.skipped_reason == "no_durable_gameplay_failure"
    assert report.outcomes[0].verifier["checks"][0]["type"] == "inventory_delta_contains"
    with session_factory() as session:
        records = session.scalars(select(SkillRecord)).all()
        assert {record.name for record in records} == {
            "gain_height_by_pillaring",
            "recover_unreachable_by_digging",
        }
        assert all(record.source_run_id is None for record in records)
        assert session.scalar(select(LearningCandidateRecord)) is None


@pytest.mark.anyio
async def test_week10_live_training_close_timeout_does_not_abort_report(
    session_factory: sessionmaker[Session],
) -> None:
    """Runtime close failures should be audited without escaping worker tasks."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_close_timeout_test",
            auto_promote=False,
            budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: CloseTimeoutRuntime(),
        model_router_factory=lambda _task_spec: _fixed_router(
            {"type": "scan_blocks", "args": {"block": "oak_log", "count": 1}}
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "succeeded"
    assert report.success_count == 1


@pytest.mark.anyio
async def test_week10_live_training_runtime_error_reports_persisted_steps(
    session_factory: sessionmaker[Session],
) -> None:
    """Runtime-error summaries should preserve completed audited step counts."""

    actions = [
        {"type": "scan_blocks", "args": {"block": "oak_log", "count": 1}},
        {
            "type": "dig_block_at",
            "args": {"block": "oak_log", "position": {"x": 0, "y": 64, "z": 0}},
        },
    ]
    model_id = "partial-runtime-error-test"
    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_partial_error_test",
            auto_promote=False,
            budget=TrainingBudget(max_steps_per_task=2, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: RuntimeErrorAfterOneStepRuntime(),
        model_router_factory=lambda _task_spec: ModelRouter(
            default_model=model_id,
            provider=ScriptedActionProvider(actions),
            profiles={model_id: ModelProfile(id=model_id, provider="scripted", tool_json=True)},
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    outcome = report.outcomes[0]
    assert report.status == "completed_with_failures"
    assert outcome.status == "runtime_error"
    assert outcome.steps == 1
    assert "RuntimeError" in str(outcome.runtime_error)
    with session_factory() as session:
        assert len(session.scalars(select(StepRecord)).all()) == 1


@pytest.mark.anyio
async def test_live_training_keeps_action_rpc_timeout_primary_over_inconclusive_verifier(
    session_factory: sessionmaker[Session],
) -> None:
    """Creative verification must not hide an unknown worker-side action result."""

    provider = InconclusiveCreativeProvider()
    runner = LiveTrainingRunner(
        task_provider=provider,  # type: ignore[arg-type] - contract-focused provider double.
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week11_rpc_timeout_priority_test",
            auto_promote=False,
            worker_failure_requeues=0,
            budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: RpcTimeoutResultRuntime(),
        model_router_factory=lambda _task_spec: _fixed_router(
            {"type": "query_inventory", "args": {}}
        ),
    )

    report = await runner.run(["creative:test"])

    outcome = report.outcomes[0]
    assert outcome.status == "runtime_error"
    assert outcome.failure_class == "action_rpc_timeout"
    assert provider.verify_calls == 0
    with session_factory() as session:
        run = session.scalar(select(RunRecord))
        assert run is not None
        assert run.status == "runtime_error"
        errors = session.scalars(select(RuntimeErrorRecord)).all()
        assert {error.error_type for error in errors} == {"ActionRpcTimeout"}
        assert session.scalars(select(TaskMemoryRecord)).all() == []


@pytest.mark.anyio
async def test_live_training_recovers_worker_before_rpc_timeout_requeue(
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown action state should trigger worker recovery before the next attempt starts."""

    runtime_factory = RpcTimeoutThenSuccessFactory()
    recoveries: list[tuple[str, str | None]] = []

    async def recover(
        worker: LiveWorkerSpec,
        outcome: LiveTrainingOutcome,
    ) -> dict[str, Any]:
        """Record the ordering boundary represented by a successful local restart."""

        recoveries.append((worker.worker_id, outcome.failure_class))
        return {"success": True, "restart_count": len(recoveries)}

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_worker_recovery_test",
            auto_promote=False,
            worker_failure_requeues=1,
            task_retry_delay_sec=0.0,
            budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=1),
        ),
        runtime_factory=runtime_factory,
        model_router_factory=_scripted_router,
        worker_recovery_callback=recover,
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "succeeded"
    assert [outcome.status for outcome in report.attempt_outcomes] == [
        "runtime_error",
        "succeeded",
    ]
    assert recoveries == [("worker-1", "action_rpc_timeout")]
    with session_factory() as session:
        event_types = {
            event.event_type for event in session.scalars(select(TrajectoryEventRecord)).all()
        }
        assert {"worker_recovery", "task_requeued"} <= event_types


@pytest.mark.anyio
async def test_week10_live_training_reports_persisted_model_usage(
    session_factory: sessionmaker[Session],
) -> None:
    """Attempt and job reports should sum provider token usage from audited model calls."""

    model_id = "usage-report-test"
    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_usage_report_test",
            budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: PreloadedInventoryRuntime(),
        model_router_factory=lambda _task_spec: ModelRouter(
            default_model=model_id,
            provider=UsageProvider(),
            profiles={model_id: ModelProfile(id=model_id, provider="usage-test")},
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.attempt_outcomes[0].model_usage.model_call_count == 1
    assert report.attempt_outcomes[0].model_usage.input_tokens == 11
    assert report.attempt_outcomes[0].model_usage.output_tokens == 3
    assert report.attempt_outcomes[0].model_usage.total_tokens == 14
    assert report.model_usage == report.attempt_outcomes[0].model_usage


@pytest.mark.anyio
async def test_week10_live_training_resumes_after_completed_wave(
    session_factory: sessionmaker[Session],
) -> None:
    """Resume should skip checkpointed waves and finalize skills only after all waves finish."""

    captured: dict[str, object] = {}

    async def stop_after_first_wave(
        completed_wave_count: int,
        outcomes: list,
        skill_revision: str | None,
        learning_revision: str | None,
    ) -> None:
        captured.update(
            {
                "completed_wave_count": completed_wave_count,
                "outcomes": tuple(outcomes),
                "skill_revision": skill_revision,
                "learning_revision": learning_revision,
            }
        )
        raise SimulatedSchedulerCrash("stop after durable checkpoint")

    config = LiveTrainingConfig(
        job_id="week10_wave_resume_test",
        auto_promote=False,
        budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=1),
        task_waves=(("minedojo_harvest_oak_log",), ("minedojo_harvest_dirt",)),
    )
    common = {
        "task_provider": MineDojoTaskProvider(MANIFEST_DIR),
        "minecraft": LiveMinecraftConfig(host="localhost", port=25565),
        "workers": [
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        "session_factory": session_factory,
        "skill_library": SkillLibrary(session_factory=session_factory),
        "config": config,
        "runtime_factory": lambda _url, _timeout: ScriptedBenchmarkRuntime(),
        "model_router_factory": _scripted_router,
    }
    first_runner = LiveTrainingRunner(
        **common,
        wave_checkpoint_callback=stop_after_first_wave,
    )

    with pytest.raises(SimulatedSchedulerCrash):
        await first_runner.run(["minedojo_harvest_oak_log", "minedojo_harvest_dirt"])

    resume_state = LiveTrainingResumeState(
        completed_wave_count=int(captured["completed_wave_count"]),
        attempt_outcomes=captured["outcomes"],
        skill_snapshot_revision=captured["skill_revision"],
        learning_snapshot_revision=captured["learning_revision"],
    )
    resumed_runner = LiveTrainingRunner(**common, resume_state=resume_state)
    report = await resumed_runner.run(["minedojo_harvest_oak_log", "minedojo_harvest_dirt"])

    assert report.status == "succeeded"
    assert report.wave_count == 2
    assert report.task_count == 2
    assert report.attempt_count == 2
    assert [outcome.task_id for outcome in report.outcomes] == [
        "minedojo_harvest_oak_log",
        "minedojo_harvest_dirt",
    ]
    with session_factory() as session:
        runs = session.scalars(select(RunRecord)).all()
        assert len(runs) == 2


class SimulatedSchedulerCrash(RuntimeError):
    """Test-only exception raised immediately after a durable wave checkpoint."""


@pytest.mark.anyio
async def test_week10_live_training_model_timeout_is_inconclusive(
    session_factory: sessionmaker[Session],
) -> None:
    """Model timeout exhaustion should not be stored as task failure memory."""

    model_id = "always-timeout-test"
    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_model_timeout_test",
            auto_promote=False,
            budget=TrainingBudget(max_steps_per_task=1, worker_concurrency=1),
            model_timeout_retries=1,
            model_timeout_backoff_sec=(0.0,),
            model_timeout_requeues=0,
        ),
        runtime_factory=lambda _url, _timeout: PreloadedInventoryRuntime(),
        model_router_factory=lambda _task_spec: ModelRouter(
            default_model=model_id,
            provider=AlwaysTimeoutProvider(),
            profiles={model_id: ModelProfile(id=model_id, provider="timeout-test", tool_json=True)},
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    outcome = report.outcomes[0]
    assert report.status == "completed_with_inconclusive"
    assert report.success_count == 0
    assert outcome.status == "model_timeout"
    assert outcome.verifier["inconclusive"] is True
    assert outcome.steps == 0
    assert outcome.learning_update is not None
    assert outcome.learning_update.skipped_reason == "excluded_run_status:model_timeout"
    with session_factory() as session:
        run = session.get(RunRecord, outcome.run_id)
        assert run is not None
        assert run.status == "model_timeout"
        assert session.scalars(select(TaskMemoryRecord)).all() == []


@pytest.mark.anyio
async def test_week10_live_training_task_timeout_is_inconclusive(
    session_factory: sessionmaker[Session],
) -> None:
    """Task runtime budget exhaustion should not be reported as a worker crash."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_task_timeout_test",
            auto_promote=False,
            budget=TrainingBudget(
                max_steps_per_task=1,
                max_runtime_sec_per_task=0.001,
                worker_concurrency=1,
            ),
        ),
        runtime_factory=lambda _url, _timeout: SlowObserveRuntime(),
        model_router_factory=lambda _task_spec: _fixed_router(
            {"type": "query_inventory", "args": {}}
        ),
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    outcome = report.outcomes[0]
    assert report.status == "completed_with_inconclusive"
    assert outcome.status == "task_timeout"
    assert outcome.verifier["inconclusive"] is True
    assert "max_runtime_sec_per_task" in str(outcome.runtime_error)
    with session_factory() as session:
        assert session.scalars(select(TaskMemoryRecord)).all() == []


@pytest.mark.anyio
async def test_week10_live_training_cancellation_finalizes_active_run(
    session_factory: sessionmaker[Session],
) -> None:
    """Cancelling a live job should persist a terminal run instead of leaving it running."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_cancelled_test",
            auto_promote=False,
            budget=TrainingBudget(
                max_steps_per_task=1,
                max_runtime_sec_per_task=30.0,
                worker_concurrency=1,
            ),
        ),
        runtime_factory=lambda _url, _timeout: SlowObserveRuntime(),
        model_router_factory=lambda _task_spec: _fixed_router(
            {"type": "query_inventory", "args": {}}
        ),
    )

    job = asyncio.create_task(runner.run(["minedojo_harvest_oak_log"]))
    await asyncio.sleep(0.01)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    with session_factory() as session:
        run = session.scalar(select(RunRecord))
        interrupted = session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.event_type == "run_interrupted"
            )
        )
    assert run is not None
    assert run.status == "cancelled"
    assert run.finished_at is not None
    assert interrupted is not None


@pytest.mark.anyio
async def test_week10_live_training_requeues_model_timeout_once(
    session_factory: sessionmaker[Session],
) -> None:
    """A model-timeout attempt should be audited and retried once without polluting memory."""

    router_factory = TimeoutThenScriptedRouterFactory()
    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_model_timeout_requeue_test",
            auto_promote=False,
            budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=1),
            model_timeout_retries=0,
            model_timeout_backoff_sec=(0.0,),
            model_timeout_requeues=1,
            model_timeout_requeue_delay_sec=0.0,
        ),
        runtime_factory=lambda _url, _timeout: ScriptedBenchmarkRuntime(),
        model_router_factory=router_factory,
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "succeeded"
    assert report.success_count == 1
    assert len(report.outcomes) == 1
    assert len(report.attempt_outcomes) == 2
    assert report.attempt_count == 2
    assert report.retried_task_count == 1
    assert [outcome.attempt for outcome in report.attempt_outcomes] == [1, 2]
    assert report.attempt_outcomes[0].status == "model_timeout"
    assert report.outcomes[0].status == "succeeded"
    assert router_factory.calls == 2
    with session_factory() as session:
        statuses = sorted(record.status for record in session.scalars(select(RunRecord)).all())
        assert statuses == ["model_timeout", "succeeded"]
        assert session.scalars(select(TaskMemoryRecord)).all() == []


@pytest.mark.anyio
async def test_week10_live_training_retries_runtime_failure_and_keeps_attempt_audit(
    session_factory: sessionmaker[Session],
) -> None:
    """A generic retry should preserve the failure attempt and stop after later success."""

    runtime_factory = RuntimeErrorThenSuccessFactory()
    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
                server_id="server-1",
                minecraft_host="localhost",
                minecraft_port=25565,
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_generic_retry_test",
            auto_promote=False,
            budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=1),
            max_task_retries=1,
            task_retry_delay_sec=0.0,
        ),
        runtime_factory=runtime_factory,
        model_router_factory=_scripted_router,
    )

    report = await runner.run(["minedojo_harvest_oak_log"])

    assert report.status == "succeeded"
    assert report.task_count == 1
    assert report.attempt_count == 2
    assert report.retried_task_count == 1
    assert report.success_count == 1
    assert [outcome.status for outcome in report.attempt_outcomes] == [
        "runtime_error",
        "succeeded",
    ]
    assert len({outcome.memory_namespace for outcome in report.attempt_outcomes}) == 1
    assert report.outcomes[0] == report.attempt_outcomes[1]
    with session_factory() as session:
        requeues = session.scalars(
            select(TrajectoryEventRecord).where(TrajectoryEventRecord.event_type == "task_requeued")
        ).all()
        assert len(requeues) == 1
        assert requeues[0].payload["reason"] == "runtime_error"
        assert requeues[0].payload["from_attempt"] == 1
        assert requeues[0].payload["to_attempt"] == 2


def test_week10_live_training_injects_clear_inventory_reset_policy(
    session_factory: sessionmaker[Session],
) -> None:
    """Validate live reset can clear verifier target items before the first observation."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        config=LiveTrainingConfig(clear_inventory_on_reset=True),
    )
    task_spec = {
        "task_id": "minedojo_harvest_oak_log",
        "verifier": {"type": "inventory_contains", "item": "oak_log", "count": 1},
    }
    request = TrainingTaskRequest.build(
        job_id="week10_reset_policy_test",
        task_id="minedojo_harvest_oak_log",
        attempt=1,
        runtime_profile="test",
    )

    live_spec = runner._live_task_spec(task_spec, request, runner.workers[0], "run-reset")

    assert live_spec["runtime"]["reset_policy"]["clear_inventory"] == {
        "enabled": True,
        "mode": "items",
        "items": ["oak_log"],
        "wait_ms": 750,
        "drop_fallback": True,
    }


def test_week10_live_training_injects_random_teleport_reset_plan(
    session_factory: sessionmaker[Session],
) -> None:
    """Validate live reset can randomize bot spawn location through RCON spreadplayers."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        config=LiveTrainingConfig(
            random_teleport_reset=RandomTeleportResetConfig(
                enabled=True,
                center_x=100,
                center_z=-50,
                spread_distance=4,
                max_range=300,
            )
        ),
    )
    task_spec = {
        "task_id": "harvest_1_feather",
        "reset_plan": {
            "start_position": {"x": 1, "y": 80, "z": 2},
            "set_time": "day",
        },
    }
    request = TrainingTaskRequest.build(
        job_id="week10_random_teleport_test",
        task_id="harvest_1_feather",
        attempt=1,
        runtime_profile="test",
    )

    live_spec = runner._live_task_spec(task_spec, request, runner.workers[0], "run-random-tp")

    reset_plan = live_spec["reset_plan"]
    assert "start_position" not in reset_plan
    assert reset_plan["set_time"] == "day"
    assert reset_plan["random_teleport"] == {
        "enabled": True,
        "center": {"x": 100, "z": -50},
        "spread_distance": 4,
        "max_range": 300,
    }
    assert reset_plan["notes"] == [
        "Live runner override: random teleport on reset was enabled for environment isolation."
    ]


def test_week10_random_teleport_fallback_preserves_explicit_biome(
    session_factory: sessionmaker[Session],
) -> None:
    """Missing-biome fallback must not override a MineDojo specified biome."""

    runner = LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        config=LiveTrainingConfig(
            random_teleport_reset=RandomTeleportResetConfig(
                enabled=True,
                only_when_biome_missing=True,
            )
        ),
    )

    biome_plan = runner._reset_plan(
        {"reset_plan": {"biome_hint": "plains", "random_teleport": {"enabled": False}}}
    )
    fallback_plan = runner._reset_plan({"reset_plan": {"set_time": "day"}})

    assert biome_plan == {
        "biome_hint": "plains",
        "random_teleport": {"enabled": False},
    }
    assert fallback_plan["random_teleport"]["enabled"] is True


def _runner(
    session_factory: sessionmaker[Session],
    *,
    auto_promote: bool,
) -> LiveTrainingRunner:
    """Build a live training runner backed by deterministic fake runtime."""

    return LiveTrainingRunner(
        task_provider=MineDojoTaskProvider(MANIFEST_DIR),
        minecraft=LiveMinecraftConfig(host="localhost", port=25565),
        workers=[
            LiveWorkerSpec(
                worker_id="worker-1",
                worker_url="fake://worker-1",
                username="Trainer1",
            )
        ],
        session_factory=session_factory,
        skill_library=SkillLibrary(session_factory=session_factory),
        config=LiveTrainingConfig(
            job_id="week10_live_test",
            auto_promote=auto_promote,
            budget=TrainingBudget(max_steps_per_task=7, worker_concurrency=1),
        ),
        runtime_factory=lambda _url, _timeout: ScriptedBenchmarkRuntime(),
        model_router_factory=_scripted_router,
    )


def _scripted_router(task_spec: dict[str, object]) -> ModelRouter:
    """Build a scripted router from one task spec's benchmark action list."""

    benchmark = task_spec.get("benchmark") if isinstance(task_spec.get("benchmark"), dict) else {}
    actions = list(benchmark.get("scripted_actions", []))
    model_id = "scripted-week10-live-test"
    return ModelRouter(
        default_model=model_id,
        provider=ScriptedActionProvider(actions),
        profiles={model_id: ModelProfile(id=model_id, provider="scripted", tool_json=True)},
    )


def _fixed_router(action: dict[str, object]) -> ModelRouter:
    """Build a scripted router that returns one fixed action."""

    model_id = "fixed-week10-live-test"
    return ModelRouter(
        default_model=model_id,
        provider=ScriptedActionProvider([action]),
        profiles={model_id: ModelProfile(id=model_id, provider="scripted", tool_json=True)},
    )


class TimeoutThenScriptedRouterFactory:
    """Router factory that times out first attempt and succeeds on retry."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, task_spec: dict[str, object]) -> ModelRouter:
        """Return a timeout router once, then a scripted manifest router."""

        self.calls += 1
        if self.calls == 1:
            model_id = "timeout-first-attempt-test"
            return ModelRouter(
                default_model=model_id,
                provider=AlwaysTimeoutProvider(),
                profiles={
                    model_id: ModelProfile(
                        id=model_id,
                        provider="timeout-test",
                        tool_json=True,
                    )
                },
            )
        return _scripted_router(task_spec)


class RuntimeErrorThenSuccessFactory:
    """Runtime factory that fails one attempt and succeeds on the next one."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _url: str, _timeout: float) -> GameRuntime:
        """Return an always-failing runtime once, then the scripted success runtime."""

        self.calls += 1
        if self.calls == 1:
            return RuntimeErrorAfterOneStepRuntime()
        return ScriptedBenchmarkRuntime()


class InconclusiveCreativeProvider:
    """Task-provider double whose verifier would be inconclusive if it were called."""

    def __init__(self) -> None:
        self.verify_calls = 0

    async def load_task(self, task_id: str) -> dict[str, object]:
        """Return one executable creative task without catalog-only markers."""

        return {
            "task_id": task_id,
            "category": "creative",
            "goal": "Build a visible test object.",
            "verifier": {"type": "creative_mineclip"},
        }

    async def verify(self, run_state: dict[str, object]) -> dict[str, object]:
        """Return an inconclusive result while counting unexpected verifier calls."""

        _ = run_state
        self.verify_calls += 1
        return {
            "success": False,
            "inconclusive": True,
            "reason": "offline visual scoring pending",
            "checks": [],
        }


class RpcTimeoutResultRuntime(GameRuntime):
    """Runtime double matching MineflayerClient's responsive timeout payload."""

    async def reset(self, task_spec: dict[str, object]) -> None:
        """Accept the creative task reset."""

        _ = task_spec

    async def observe(self) -> dict[str, object]:
        """Return a stable world observation before the unknown action result."""

        return {
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [],
            "nearby_entities": [],
        }

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Return an RPC timeout while proving the worker still answered observe."""

        return {
            "ok": False,
            "action_type": action.type,
            "error_code": "rpc_timeout",
            "message": "worker action result was not returned",
            "recoverable": True,
            "terminated": False,
            "requires_worker_restart": True,
            "worker_health": {"responsive": True},
            "observation": await self.observe(),
        }

    async def snapshot(self) -> dict[str, object]:
        """Return an empty runtime snapshot."""

        return {"image": None}

    async def close(self) -> None:
        """Close without external resources."""


class RpcTimeoutThenSuccessFactory:
    """Runtime factory that exposes one unknown action state before a clean retry."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _url: str, _timeout: float) -> GameRuntime:
        """Return the RPC-timeout runtime once and the benchmark runtime afterward."""

        self.calls += 1
        if self.calls == 1:
            return RpcTimeoutResultRuntime()
        return ScriptedBenchmarkRuntime()


class AlwaysTimeoutProvider:
    """Fake provider that always times out before returning model content."""

    async def complete(
        self,
        messages: list[dict[str, object]],
        profile: ModelProfile,
        response_schema: dict[str, object] | None = None,
    ) -> ModelCompletion:
        """Simulate a provider read timeout."""

        _ = (messages, profile, response_schema)
        raise TimeoutError("model read timed out")


class UsageProvider:
    """Fake provider that returns one action with non-zero audited token usage."""

    async def complete(
        self,
        messages: list[dict[str, object]],
        profile: ModelProfile,
        response_schema: dict[str, object] | None = None,
    ) -> ModelCompletion:
        """Return a deterministic inventory action and provider usage counters."""

        _ = (messages, profile, response_schema)
        return ModelCompletion(
            content='{"type":"query_inventory","args":{}}',
            usage=ModelUsage(input_tokens=11, output_tokens=3, total_tokens=14),
        )


class PreloadedInventoryRuntime(GameRuntime):
    """Fake runtime with target inventory already present before any action."""

    async def reset(self, task_spec: dict[str, object]) -> None:
        """Accept reset without mutating the preloaded inventory."""

        _ = task_spec

    async def observe(self) -> dict[str, object]:
        """Return an observation already containing the target item."""

        return {
            "health": 20,
            "food": 20,
            "inventory": [{"name": "oak_log", "count": 1}],
            "nearby_blocks": [],
            "nearby_entities": [],
        }

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Only support inventory inspection for this regression test."""

        return {
            "ok": True,
            "action_type": action.type,
            "inventory": [{"name": "oak_log", "count": 1}],
            "observation": await self.observe(),
        }

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""


class SlowObserveRuntime(GameRuntime):
    """Fake runtime that exceeds the task-level wall-clock budget."""

    async def reset(self, task_spec: dict[str, object]) -> None:
        """Accept reset immediately."""

        _ = task_spec

    async def observe(self) -> dict[str, object]:
        """Sleep long enough for asyncio.wait_for to expire."""

        import asyncio

        await asyncio.sleep(0.05)
        return {"inventory": [], "nearby_blocks": [], "nearby_entities": []}

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Return a no-op action result."""

        return {"ok": True, "action_type": action.type, "observation": await self.observe()}

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""


class CloseTimeoutRuntime(GameRuntime):
    """Fake runtime that succeeds but raises while closing."""

    def __init__(self) -> None:
        self.inventory: list[dict[str, object]] = []

    async def reset(self, task_spec: dict[str, object]) -> None:
        """Reset to an empty inventory with a nearby oak log."""

        _ = task_spec
        self.inventory = []

    async def observe(self) -> dict[str, object]:
        """Return the current fake observation."""

        return {
            "health": 20,
            "food": 20,
            "inventory": list(self.inventory),
            "nearby_blocks": [{"name": "oak_log", "position": {"x": 1, "y": 65, "z": 0}}],
            "nearby_entities": [],
        }

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Mine one oak log successfully."""

        self.inventory = [{"name": "oak_log", "count": 1}]
        return {
            "ok": True,
            "action_type": action.type,
            "block": "oak_log",
            "inventory_delta": {"oak_log": 1},
            "observation": await self.observe(),
        }

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Simulate a close timeout after the task has already finished."""

        raise TimeoutError("close timed out")


class RuntimeErrorAfterOneStepRuntime(GameRuntime):
    """Fake runtime that persists one completed step, then fails the next action."""

    def __init__(self) -> None:
        self.action_count = 0

    async def reset(self, task_spec: dict[str, object]) -> None:
        """Accept reset for a deterministic partial-progress run."""

        _ = task_spec

    async def observe(self) -> dict[str, object]:
        """Return a simple harvest observation."""

        return {
            "health": 20,
            "food": 20,
            "inventory": [],
            "nearby_blocks": [{"name": "oak_log", "position": {"x": 1, "y": 65, "z": 0}}],
            "nearby_entities": [],
        }

    async def act(self, action: HarnessAction) -> dict[str, object]:
        """Succeed once, then simulate a worker crash."""

        if self.action_count == 0:
            self.action_count += 1
            return {
                "ok": True,
                "action_type": action.type,
                "blocks": [{"name": "oak_log", "position": {"x": 1, "y": 65, "z": 0}}],
                "observation": await self.observe(),
            }
        raise RuntimeError("worker action crashed")

    async def snapshot(self) -> dict[str, object]:
        """Return an empty snapshot placeholder."""

        return {"image": None, "format": None}

    async def close(self) -> None:
        """Close the fake runtime."""
