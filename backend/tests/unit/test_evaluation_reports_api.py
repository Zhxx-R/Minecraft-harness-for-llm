from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mc_agent_harness.api.routes.dashboard import get_session
from mc_agent_harness.db.models import (
    Base,
    ModelCallRecord,
    RunRecord,
    RuntimeErrorRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.main import create_app


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an isolated database for evaluation report tests."""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> TestClient:
    """Create an API client using the isolated report database."""

    app = create_app()

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_evaluation_reports_aggregate_persisted_runs(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Reports aggregate verifier outcomes, usage, duration, categories, and skill cohorts."""

    _seed_evaluation_rows(session_factory)

    response = client.get("/api/evaluation-reports?recent_limit=3")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total_runs": 5,
        "unique_tasks": 5,
        "succeeded": 1,
        "failed": 1,
        "cancelled": 1,
        "running": 1,
        "unverified": 1,
        "success_rate": 0.5,
        "total_steps": 3,
        "model_calls": 4,
        "runtime_errors": 1,
        "input_tokens": 180,
        "output_tokens": 35,
        "total_tokens": 224,
        "estimated_cost": 0.32,
        "duration_seconds": 390.0,
        "avg_duration_sec": 97.5,
        "avg_steps_per_run": 0.6,
    }

    categories = {row["category"]: row for row in payload["by_category"]}
    assert categories["harvest"]["run_count"] == 2
    assert categories["harvest"]["succeeded"] == 1
    assert categories["harvest"]["unverified"] == 1
    assert categories["harvest"]["success_rate"] == 1.0
    assert categories["combat"]["failed"] == 1
    assert categories["creative"]["running"] == 1
    assert categories["techtree"]["cancelled"] == 1

    skill_modes = {row["mode"]: row for row in payload["by_skill_usage"]}
    assert skill_modes["skill_injected"]["run_count"] == 1
    assert skill_modes["skill_injected"]["succeeded"] == 1
    assert skill_modes["no_skill_injected"]["run_count"] == 4

    assert len(payload["recent_runs"]) == 3
    assert payload["recent_runs"][0]["run_id"] == "run_unverified"
    interrupted = next(row for row in payload["recent_runs"] if row["run_id"] == "run_interrupted")
    assert interrupted["lifecycle_status"] == "interrupted"
    assert interrupted["task_result"] == "not_evaluated"
    assert interrupted["result_bucket"] == "cancelled"


def test_evaluation_reports_do_not_guess_model_cost(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Token usage without an explicit persisted cost leaves report cost unknown."""

    started_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_without_cost",
                task_id="harvest_without_cost",
                status="succeeded",
                task_spec={"category": "harvest"},
                started_at=started_at,
                finished_at=started_at + timedelta(seconds=10),
            )
        )
        session.add(
            ModelCallRecord(
                run_id="run_without_cost",
                step_index=0,
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
        )
        session.commit()

    payload = client.get("/api/evaluation-reports").json()

    assert payload["summary"]["total_tokens"] == 12
    assert payload["summary"]["estimated_cost"] is None
    assert payload["recent_runs"][0]["estimated_cost"] is None


def test_evaluation_reports_return_empty_metrics(
    client: TestClient,
) -> None:
    """An empty database returns a renderable report instead of a missing-data error."""

    response = client.get("/api/evaluation-reports")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total_runs"] == 0
    assert payload["summary"]["success_rate"] is None
    assert payload["summary"]["estimated_cost"] is None
    assert payload["summary"]["avg_duration_sec"] is None
    assert payload["summary"]["avg_steps_per_run"] is None
    assert payload["by_category"] == []
    assert [row["mode"] for row in payload["by_skill_usage"]] == [
        "skill_injected",
        "no_skill_injected",
    ]
    assert payload["recent_runs"] == []


def _seed_evaluation_rows(session_factory: sessionmaker[Session]) -> None:
    """Insert five runs covering every top-level report result bucket."""

    base = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    run_specs = [
        (
            "run_success",
            "harvest_white_wool",
            "running",
            "harvest",
            base,
            base + timedelta(seconds=120),
        ),
        (
            "run_failed",
            "combat_zombie",
            "completed",
            "combat",
            base + timedelta(hours=1),
            base + timedelta(hours=1, seconds=180),
        ),
        (
            "run_running",
            "creative_small_house",
            "running",
            "creative",
            base + timedelta(hours=2),
            None,
        ),
        (
            "run_interrupted",
            "techtree_craft_pickaxe",
            "interrupted",
            "techtree",
            base + timedelta(hours=3),
            base + timedelta(hours=3, seconds=60),
        ),
        (
            "run_unverified",
            "harvest_oak_log_unverified",
            "completed_unverified",
            "harvest",
            base + timedelta(hours=4),
            base + timedelta(hours=4, seconds=30),
        ),
    ]
    with session_factory() as session:
        for run_id, task_id, status, category, started_at, finished_at in run_specs:
            session.add(
                RunRecord(
                    id=run_id,
                    task_id=task_id,
                    status=status,
                    task_spec={"task_id": task_id, "category": category},
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )

        session.add_all(
            [
                StepRecord(
                    run_id="run_success",
                    step_index=0,
                    observation={},
                    action={},
                    action_result={"ok": True},
                ),
                StepRecord(
                    run_id="run_success",
                    step_index=1,
                    observation={},
                    action={},
                    action_result={"ok": True},
                ),
                StepRecord(
                    run_id="run_failed",
                    step_index=0,
                    observation={},
                    action={},
                    action_result={"ok": False},
                ),
            ]
        )
        session.add_all(
            [
                TrajectoryEventRecord(
                    run_id="run_success",
                    event_type="verifier_result",
                    payload={"success": True},
                ),
                TrajectoryEventRecord(
                    run_id="run_success",
                    event_type="context_built",
                    payload={
                        "skill_injection": {
                            "newly_injected": [{"identity": "harvest_white_wool@0.1.0"}]
                        }
                    },
                ),
                TrajectoryEventRecord(
                    run_id="run_failed",
                    event_type="verifier_result",
                    payload={"verifier": {"success": False}},
                ),
            ]
        )
        session.add_all(
            [
                ModelCallRecord(
                    run_id="run_success",
                    step_index=0,
                    usage={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "cost": 0.25,
                    },
                ),
                ModelCallRecord(
                    run_id="run_success",
                    step_index=1,
                    usage={"input_tokens": 30, "output_tokens": 10, "cost": 0.0},
                ),
                ModelCallRecord(
                    run_id="run_failed",
                    step_index=0,
                    usage={
                        "input_tokens": 50,
                        "output_tokens": 5,
                        "total_tokens": 55,
                        "estimated_cost": "0.05",
                    },
                ),
                ModelCallRecord(
                    run_id="run_running",
                    step_index=0,
                    usage={"total_tokens": 9, "cost": 0.02},
                ),
            ]
        )
        session.add(
            RuntimeErrorRecord(
                run_id="run_failed",
                step_index=0,
                error_type="worker_timeout",
                message="runtime did not respond",
                payload={},
            )
        )
        session.commit()
