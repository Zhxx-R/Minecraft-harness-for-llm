from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mc_agent_harness.api.routes.dashboard import get_session
from mc_agent_harness.db.models import (
    Base,
    LearningCandidateRecord,
    ModelCallRecord,
    RunRecord,
    RuntimeErrorRecord,
    SkillRecord,
    StepRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.main import create_app
from mc_agent_harness.schemas.action import HarnessAction
from mc_agent_harness.schemas.skill import SkillSpec, SkillStatus
from mc_agent_harness.skills.initial import seed_initial_skills
from mc_agent_harness.skills.library import SkillLibrary


_CONTROL_HEADERS = {"X-Harness-Control": "local-dashboard-v1"}


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an in-memory SQLite session factory for dashboard route tests."""

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
    """Create a FastAPI test client whose dashboard routes use the test database."""

    app = create_app()

    def override_session() -> Iterator[Session]:
        """Yield a test database session for one request."""

        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_dashboard_reads_persisted_run_audit_views(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Dashboard endpoints expose run summaries, events, model calls, and runtime errors."""

    _seed_dashboard_rows(session_factory)

    runs = client.get("/api/runs")
    assert runs.status_code == 200
    assert runs.json()[0]["id"] == "run_dashboard"
    assert runs.json()[0]["lifecycle_status"] == "running"
    assert runs.json()[0]["task_result"] == "succeeded"
    assert runs.json()[0]["verifier_success"] is True
    assert runs.json()[0]["event_count"] == 8
    assert runs.json()[0]["model_call_count"] == 1
    assert runs.json()[0]["runtime_error_count"] == 1

    detail = client.get("/api/runs/run_dashboard")
    assert detail.status_code == 200
    assert detail.json()["task_id"] == "minedojo_harvest_oak_log"
    assert detail.json()["lifecycle_status"] == "running"
    assert detail.json()["task_result"] == "succeeded"

    events = client.get("/api/runs/run_dashboard/events")
    assert events.status_code == 200
    assert "model_action" in {event["event_type"] for event in events.json()}

    model_calls = client.get("/api/runs/run_dashboard/model-calls")
    assert model_calls.status_code == 200
    assert model_calls.json()[0]["action"]["type"] == "dig_block_at"

    runtime_errors = client.get("/api/runs/run_dashboard/runtime-errors")
    assert runtime_errors.status_code == 200
    assert runtime_errors.json()[0]["error_type"] == "worker_timeout"


def test_dashboard_exposes_failure_learning_candidates(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Learning candidates remain separately reviewable from promoted skills."""

    with session_factory() as session:
        candidate = LearningCandidateRecord(
            signature="combat:enderman:engage_combat:target_lost",
            scope_key="combat:enderman",
            kind="combat_adaptation",
            status="validated",
            hypothesis="Reacquire the target before retrying combat.",
            failure_status="target_lost",
            action_type="engage_combat",
            target="enderman",
            support_count=2,
            recovery_count=1,
            contradiction_count=0,
            confidence=0.8,
            evidence={"validated_recovery": {"run_id": "recovery-run"}},
            knowledge_refs=[{"tool": "retrieve_docs", "query": "enderman behavior"}],
            source_run_ids=["failed-run"],
            recovery_run_ids=["recovery-run"],
        )
        session.add(candidate)
        session.commit()
        candidate_id = candidate.id

    listed = client.get("/api/learning-candidates?status=validated")
    detailed = client.get(f"/api/learning-candidates/{candidate_id}")

    assert listed.status_code == 200
    assert listed.json()[0]["scope_key"] == "combat:enderman"
    assert listed.json()[0]["recovery_count"] == 1
    assert detailed.status_code == 200
    assert detailed.json()["knowledge_refs"][0]["query"] == "enderman behavior"


def test_dashboard_returns_agent_audit_snapshot(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Agent audit endpoint exposes identity, state, latest action, and token totals."""

    _seed_dashboard_rows(session_factory)

    response = client.get("/api/runs/run_dashboard/agent-audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["presence"] == "online"
    assert payload["lifecycle_status"] == "running"
    assert payload["task_result"] == "succeeded"
    assert payload["verifier_success"] is True
    assert payload["identity"]["username"] == "HarnessTrainer1"
    assert payload["identity"]["worker_id"] == "worker-1"
    assert payload["identity"]["agent_id"] == "agent-1"
    assert payload["current_task"]["task_id"] == "minedojo_harvest_oak_log"
    assert payload["latest_observation"] == {"inventory": []}
    assert payload["latest_action"]["type"] == "dig_block_at"
    assert payload["latest_action_result"]["ok"] is True
    assert payload["latest_model_output"] == '{"type":"dig_block_at"}'
    assert payload["latest_model_usage"]["total_tokens"] == 12
    assert payload["token_totals"]["input_tokens"] == 7
    assert payload["token_totals"]["total_tokens"] == 12
    assert payload["reset"]["reset_policy"]["clear_inventory"]["verified"] is True
    assert payload["verifier"]["success"] is True
    assert payload["runtime_error_count"] == 1
    assert payload["latest_runtime_error"]["error_type"] == "worker_timeout"
    assert payload["event_counts"]["model_action"] == 1


def test_dashboard_returns_agent_overview_and_detail(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Agent endpoints group persisted runs into an auditable agent detail page."""

    _seed_dashboard_rows(session_factory)

    agents = client.get("/api/agents")
    assert agents.status_code == 200
    overview = agents.json()[0]
    assert overview["key"] == "username:HarnessTrainer1"
    assert overview["display_name"] == "HarnessTrainer1"
    assert overview["run_count"] == 1
    assert overview["skill_count"] == 1
    assert overview["token_totals"]["total_tokens"] == 12
    assert overview["latest_task_result"] == "succeeded"
    assert overview["task_success_count"] == 1
    assert overview["task_failure_count"] == 0

    detail = client.get("/api/agents/username%3AHarnessTrainer1")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["agent"]["username"] == "HarnessTrainer1"
    assert payload["runs"][0]["run_id"] == "run_dashboard"
    assert payload["runs"][0]["lifecycle_status"] == "running"
    assert payload["runs"][0]["task_result"] == "succeeded"
    assert payload["runs"][0]["verifier_success"] is True
    assert payload["runs"][0]["verifier"]["success"] is True
    assert payload["skills"][0]["name"] == "collect_wood"


def test_dashboard_distinguishes_lifecycle_from_task_result(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A completed run can still have a failed verifier result."""

    _seed_completed_failed_run(session_factory)

    detail = client.get("/api/runs/run_completed_failed")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["status"] == "completed"
    assert payload["lifecycle_status"] == "completed"
    assert payload["task_result"] == "failed"
    assert payload["verifier_success"] is False

    audit = client.get("/api/runs/run_completed_failed/agent-audit")
    assert audit.status_code == 200
    assert audit.json()["presence"] == "finished"
    assert audit.json()["task_result"] == "failed"


def test_dashboard_marks_agent_finish_without_evaluator_as_unverified(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """An agent-requested stop must remain distinct from evaluated success."""

    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_unverified_finish",
                task_id="ad_hoc_build",
                status="completed_unverified",
                task_spec={"task_id": "ad_hoc_build", "goal": "Build something useful."},
                finished_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_unverified_finish",
                event_type="run_finished",
                payload={
                    "task_id": "ad_hoc_build",
                    "terminated": True,
                    "stop_reason": "agent_finished_unverified",
                },
            )
        )
        session.commit()

    detail = client.get("/api/runs/run_unverified_finish")

    assert detail.status_code == 200
    assert detail.json()["lifecycle_status"] == "completed_unverified"
    assert detail.json()["task_result"] == "unverified"
    assert detail.json()["verifier_success"] is None


def test_dashboard_streams_trajectory_events(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Run event stream emits persisted trajectory events as SSE frames."""

    _seed_dashboard_rows(session_factory)

    response = client.get("/api/runs/run_dashboard/stream?close_after_current_batch=true")

    assert response.status_code == 200
    body = response.text
    assert "event: trajectory" in body
    assert '"event_type":"run_started"' in body
    assert '"run_id":"run_dashboard"' in body


def test_dashboard_stream_respects_after_id(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Run event stream starts after the supplied trajectory event id."""

    _seed_dashboard_rows(session_factory)
    first_event_id = _first_event_id(session_factory, "run_dashboard")

    response = client.get(
        f"/api/runs/run_dashboard/stream?after_id={first_event_id}&close_after_current_batch=true"
    )

    assert response.status_code == 200
    body = response.text
    assert "event: trajectory" in body
    assert '"event_type":"run_started"' not in body
    assert '"event_type":"environment_reset"' in body


def test_dashboard_promotes_and_deprecates_skill(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Skill review endpoints mutate lifecycle state while preserving source audit metadata."""

    _seed_dashboard_rows(session_factory)

    skills = client.get("/api/skills")
    assert skills.status_code == 200
    skill_id = int(skills.json()[0]["id"])
    detail = client.get(f"/api/skills/{skill_id}").json()

    promoted = client.post(
        f"/api/skills/{skill_id}/promote",
        headers=_CONTROL_HEADERS,
        json={"expected_updated_at": detail["updated_at"]},
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"

    deprecated = client.post(
        f"/api/skills/{skill_id}/deprecate",
        headers=_CONTROL_HEADERS,
        json={
            "expected_updated_at": promoted.json()["updated_at"],
            "reason": "test drift",
        },
    )
    assert deprecated.status_code == 200
    assert deprecated.json()["status"] == "deprecated"

    with session_factory() as session:
        record = session.get(SkillRecord, skill_id)
        assert record is not None
        assert record.status == SkillStatus.deprecated.value
        spec = SkillSpec.model_validate(record.spec)
        assert spec.metrics["deprecation_reason"] == "test drift"
        event_types = [event.event_type for event in session.scalars(select(TrajectoryEventRecord))]
        assert "skill_promoted" in event_types
        assert "skill_deprecated" in event_types


def test_dashboard_reads_legacy_skill_specs_without_rewriting_evidence(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Legacy action names remain reviewable while lifecycle mutations stay strict."""

    with session_factory() as session:
        legacy = SkillRecord(
            name="legacy_harvest",
            version="0.0.1",
            status="active",
            spec={
                "description": "Historical skill with an obsolete action name.",
                "triggers": ["stone"],
                "action_plan": [{"type": "mine_block", "args": {"block": "stone"}}],
            },
        )
        session.add(legacy)
        session.commit()
        skill_id = legacy.id

    skills = client.get("/api/skills")
    assert skills.status_code == 200
    assert skills.json()[0]["description"] == (
        "Historical skill with an obsolete action name."
    )
    assert skills.json()[0]["action_count"] == 1

    detail = client.get(f"/api/skills/{skill_id}")
    assert detail.status_code == 200
    assert detail.json()["spec"]["action_plan"][0]["type"] == "mine_block"

    promoted = client.post(
        f"/api/skills/{skill_id}/promote",
        headers=_CONTROL_HEADERS,
        json={"expected_updated_at": detail.json()["updated_at"]},
    )
    assert promoted.status_code == 409
    assert "legacy specification" in promoted.json()["detail"]


def test_dashboard_requires_local_control_for_skill_update_and_delete(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Skill mutations cannot be invoked without the explicit local-dashboard control header."""

    _seed_dashboard_rows(session_factory)
    skill = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{skill['id']}").json()

    updated = client.patch(
        f"/api/skills/{skill['id']}",
        json={"spec": detail["spec"], "expected_updated_at": detail["updated_at"]},
    )
    deleted = client.request(
        "DELETE",
        f"/api/skills/{skill['id']}",
        json={"expected_updated_at": detail["updated_at"]},
    )
    promoted = client.post(
        f"/api/skills/{skill['id']}/promote",
        json={"expected_updated_at": detail["updated_at"]},
    )
    deprecated = client.post(
        f"/api/skills/{skill['id']}/deprecate",
        json={"expected_updated_at": detail["updated_at"]},
    )

    assert updated.status_code == 403
    assert deleted.status_code == 403
    assert promoted.status_code == 403
    assert deprecated.status_code == 403
    assert client.get(f"/api/skills/{skill['id']}").status_code == 200


def test_dashboard_requires_optimistic_token_for_every_skill_mutation(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """All dashboard writes reject requests that omit the last-seen update timestamp."""

    _seed_dashboard_rows(session_factory)
    skill = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{skill['id']}").json()
    endpoint = f"/api/skills/{skill['id']}"

    responses = [
        client.patch(endpoint, headers=_CONTROL_HEADERS, json={"spec": detail["spec"]}),
        client.request("DELETE", endpoint, headers=_CONTROL_HEADERS, json={}),
        client.post(f"{endpoint}/promote", headers=_CONTROL_HEADERS, json={}),
        client.post(f"{endpoint}/deprecate", headers=_CONTROL_HEADERS, json={}),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422, 422]


def test_dashboard_updates_complete_skill_spec_with_audit_event(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """A valid full-spec edit synchronizes indexed fields and records before/after evidence."""

    _seed_dashboard_rows(session_factory)
    listed = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{listed['id']}").json()
    edited_spec = {
        **detail["spec"],
        "name": "harvest_oak_safely",
        "version": "0.2.0",
        "description": "Collect starter oak logs while preserving source evidence.",
        "triggers": ["oak_log", "starter_wood"],
    }

    response = client.patch(
        f"/api/skills/{listed['id']}",
        headers=_CONTROL_HEADERS,
        json={"spec": edited_spec, "expected_updated_at": detail["updated_at"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "harvest_oak_safely"
    assert payload["version"] == "0.2.0"
    assert payload["spec"]["description"].startswith("Collect starter oak logs")
    assert payload["source_run_id"] == "run_dashboard"
    assert payload["updated_at"] != detail["updated_at"]
    with session_factory() as session:
        record = session.get(SkillRecord, listed["id"])
        assert record is not None
        assert record.name == "harvest_oak_safely"
        assert record.version == "0.2.0"
        event = session.scalar(
            select(TrajectoryEventRecord)
            .where(TrajectoryEventRecord.event_type == "skill_updated")
            .order_by(TrajectoryEventRecord.id.desc())
        )
        assert event is not None
        assert event.run_id == "run_dashboard"
        assert event.payload["before"]["name"] == "collect_wood"
        assert event.payload["after"]["name"] == "harvest_oak_safely"
        assert event.payload["authority"] == "dashboard_operator"


def test_dashboard_preserves_imported_portable_provenance_and_extensions(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Imported source provenance can differ from the local audit run without being discarded."""

    portable = SkillSpec(
        name="imported_harvest",
        version="2.0.0",
        description="Imported harvesting guidance.",
        source_run_id="remote-source-run",
        status=SkillStatus.draft,
    )
    with session_factory() as session:
        session.add(
            RunRecord(
                id="local-import-audit",
                task_id="skill_bundle_import",
                status="completed_unverified",
                task_spec={"task_id": "skill_bundle_import"},
            )
        )
        record = SkillRecord(
            name=portable.name,
            version=portable.version,
            status=portable.status.value,
            spec={
                **portable.model_dump(mode="json"),
                "_historical_import": {
                    "bundle": "server-recovery",
                    "original_id": 91,
                },
            },
            source_run_id="local-import-audit",
        )
        session.add(record)
        session.commit()
        skill_id = record.id

    detail = client.get(f"/api/skills/{skill_id}").json()
    response = client.patch(
        f"/api/skills/{skill_id}",
        headers=_CONTROL_HEADERS,
        json={
            "spec": {
                **detail["spec"],
                "description": "Edited imported harvesting guidance.",
            },
            "expected_updated_at": detail["updated_at"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_run_id"] == "local-import-audit"
    assert payload["spec"]["source_run_id"] == "remote-source-run"
    assert payload["spec"]["_historical_import"]["original_id"] == 91
    assert payload["spec"]["_dashboard_override"] is True
    with session_factory() as session:
        event = session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.event_type == "skill_updated"
            )
        )
        assert event is not None
        assert event.run_id == "local-import-audit"


def test_dashboard_rejects_invalid_or_lifecycle_changing_skill_edits(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Edits require a valid spec and cannot bypass source or lifecycle controls."""

    _seed_dashboard_rows(session_factory)
    listed = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{listed['id']}").json()
    endpoint = f"/api/skills/{listed['id']}"

    invalid = client.patch(
        endpoint,
        headers=_CONTROL_HEADERS,
        json={
            "spec": {"description": "missing required fields"},
            "expected_updated_at": detail["updated_at"],
        },
    )
    changed_source = client.patch(
        endpoint,
        headers=_CONTROL_HEADERS,
        json={
            "spec": {**detail["spec"], "source_run_id": None},
            "expected_updated_at": detail["updated_at"],
        },
    )
    changed_status = client.patch(
        endpoint,
        headers=_CONTROL_HEADERS,
        json={
            "spec": {**detail["spec"], "status": "promoted"},
            "expected_updated_at": detail["updated_at"],
        },
    )

    assert invalid.status_code == 422
    assert changed_source.status_code == 422
    assert "immutable" in changed_source.json()["detail"]
    assert changed_status.status_code == 409
    assert "lifecycle controls" in changed_status.json()["detail"]


def test_dashboard_rejects_stale_and_duplicate_skill_updates(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Optimistic timestamps and the name/version constraint prevent lost updates."""

    _seed_dashboard_rows(session_factory)
    listed = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{listed['id']}").json()

    stale = client.patch(
        f"/api/skills/{listed['id']}",
        headers=_CONTROL_HEADERS,
        json={
            "spec": {**detail["spec"], "description": "stale edit"},
            "expected_updated_at": "2000-01-01T00:00:00Z",
        },
    )
    assert stale.status_code == 409
    stale_lifecycle = client.post(
        f"/api/skills/{listed['id']}/promote",
        headers=_CONTROL_HEADERS,
        json={"expected_updated_at": "2000-01-01T00:00:00Z"},
    )
    assert stale_lifecycle.status_code == 409

    duplicate_spec = SkillSpec(
        name="duplicate_skill",
        version="1.0.0",
        description="Already occupies this unique identity.",
        source_run_id="run_dashboard",
        status=SkillStatus.draft,
    )
    with session_factory() as session:
        session.add(
            SkillRecord(
                name=duplicate_spec.name,
                version=duplicate_spec.version,
                status=duplicate_spec.status.value,
                spec=duplicate_spec.model_dump(mode="json"),
                source_run_id="run_dashboard",
            )
        )
        session.commit()

    duplicate = client.patch(
        f"/api/skills/{listed['id']}",
        headers=_CONTROL_HEADERS,
        json={
            "spec": {
                **detail["spec"],
                "name": duplicate_spec.name,
                "version": duplicate_spec.version,
            },
            "expected_updated_at": detail["updated_at"],
        },
    )

    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]
    with session_factory() as session:
        record = session.get(SkillRecord, listed["id"])
        assert record is not None
        assert record.name == "collect_wood"
        assert session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.event_type == "skill_updated"
            )
        ) is None


def test_dashboard_deletes_skill_but_preserves_source_trajectory_and_audit(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Explicit deletion tombstones only the skill and preserves its source run."""

    _seed_dashboard_rows(session_factory)
    listed = client.get("/api/skills").json()[0]
    detail = client.get(f"/api/skills/{listed['id']}").json()

    stale = client.request(
        "DELETE",
        f"/api/skills/{listed['id']}",
        headers=_CONTROL_HEADERS,
        json={"expected_updated_at": "2000-01-01T00:00:00Z"},
    )
    assert stale.status_code == 409

    response = client.request(
        "DELETE",
        f"/api/skills/{listed['id']}",
        headers=_CONTROL_HEADERS,
        json={
            "expected_updated_at": detail["updated_at"],
            "reason": "superseded during dashboard review",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": listed["id"],
        "name": "collect_wood",
        "version": "0.1.0",
        "deleted": True,
    }
    assert client.get(f"/api/skills/{listed['id']}").status_code == 404
    assert all(item["id"] != listed["id"] for item in client.get("/api/skills").json())
    with session_factory() as session:
        assert session.get(RunRecord, "run_dashboard") is not None
        assert session.scalar(
            select(StepRecord).where(StepRecord.run_id == "run_dashboard")
        ) is not None
        event = session.scalar(
            select(TrajectoryEventRecord)
            .where(TrajectoryEventRecord.event_type == "skill_deleted")
            .order_by(TrajectoryEventRecord.id.desc())
        )
        assert event is not None
        assert event.run_id == "run_dashboard"
        assert event.payload["skill"]["id"] == listed["id"]
        assert event.payload["reason"] == "superseded during dashboard review"
        assert event.payload["authority"] == "dashboard_operator"
        tombstone = session.get(SkillRecord, listed["id"])
        assert tombstone is not None
        assert tombstone.status == "deleted"
        assert tombstone.spec["_dashboard_deleted"]["authority"] == "dashboard_operator"


def test_bootstrap_dashboard_overrides_and_tombstones_survive_reseeding(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Bootstrap seeding never reverses an operator edit or permanent deletion."""

    assert seed_initial_skills(session_factory).created == 2
    skills = client.get("/api/skills").json()
    pillaring = next(item for item in skills if item["name"] == "gain_height_by_pillaring")
    digging = next(item for item in skills if item["name"] == "recover_unreachable_by_digging")
    pillaring_detail = client.get(f"/api/skills/{pillaring['id']}").json()
    digging_detail = client.get(f"/api/skills/{digging['id']}").json()

    edited = client.patch(
        f"/api/skills/{pillaring['id']}",
        headers=_CONTROL_HEADERS,
        json={
            "spec": {
                **pillaring_detail["spec"],
                "name": "custom_height_recovery",
                "description": "Operator-tuned height recovery guidance.",
            },
            "expected_updated_at": pillaring_detail["updated_at"],
        },
    )
    assert edited.status_code == 200
    deleted = client.request(
        "DELETE",
        f"/api/skills/{digging['id']}",
        headers=_CONTROL_HEADERS,
        json={"expected_updated_at": digging_detail["updated_at"]},
    )
    assert deleted.status_code == 200

    reseeded = seed_initial_skills(session_factory)

    assert reseeded.created == 0
    assert reseeded.updated == 0
    assert reseeded.unchanged == 2
    visible = client.get("/api/skills").json()
    assert [item["name"] for item in visible] == ["custom_height_recovery"]
    assert visible[0]["description"] == "Operator-tuned height recovery guidance."
    assert client.get(f"/api/skills/{digging['id']}").status_code == 404
    snapshot = asyncio.run(
        SkillLibrary(session_factory=session_factory).capture_snapshot()
    )
    assert [skill.name for skill in snapshot.skills] == ["custom_height_recovery"]
    with session_factory() as session:
        records = list(session.scalars(select(SkillRecord)).all())
        assert len(records) == 2
        tombstone = next(record for record in records if record.id == digging["id"])
        assert tombstone.status == "deleted"


def test_dashboard_replay_groups_step_evidence(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Replay endpoint returns step-centric evidence without dropping raw audit events."""

    _seed_dashboard_rows(session_factory)

    response = client.get("/api/runs/run_dashboard/replay")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["step_count"] == 2
    assert payload["summary"]["model_call_count"] == 1
    first_step = payload["steps"][0]
    assert first_step["step_index"] == 0
    assert first_step["status"] == "ok"
    assert first_step["observation"] == {"inventory": []}
    assert first_step["resolved_terms"] == ["oak_log"]
    assert first_step["retrieved_docs"] == ["minecraft.blocks.oak_log"]
    assert first_step["retrieved_skills"] == [{"name": "collect_wood", "version": "0.1.0"}]
    assert first_step["parsed_action"]["type"] == "dig_block_at"
    assert first_step["action_result"]["ok"] is True
    assert first_step["model_events"][0]["event_type"] == "model_action"
    assert first_step["model_calls"][0]["usage"]["total_tokens"] == 12
    assert any(highlight == "result: ok" for highlight in first_step["highlights"])
    second_step = payload["steps"][1]
    assert second_step["status"] == "error"
    assert second_step["runtime_errors"][0]["error_type"] == "worker_timeout"
    assert payload["run_events"][0]["event_type"] == "run_started"


def test_dashboard_returns_404_for_missing_run(client: TestClient) -> None:
    """Missing run ids return clear 404 responses."""

    response = client.get("/api/runs/missing-run")
    assert response.status_code == 404


def _seed_dashboard_rows(session_factory: sessionmaker[Session]) -> None:
    """Insert one run with specialized audit rows and one draft skill."""

    started_at = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    action = HarnessAction(
        type="dig_block_at", args={"block": "oak_log", "position": {"x": 1, "y": 64, "z": 1}}
    )
    skill = SkillSpec(
        name="collect_wood",
        version="0.1.0",
        description="Collect starter oak logs.",
        triggers=["oak_log", "wood"],
        action_plan=[action],
        source_run_id="run_dashboard",
        status=SkillStatus.draft,
    )
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_dashboard",
                task_id="minedojo_harvest_oak_log",
                status="running",
                task_spec={
                    "task_id": "minedojo_harvest_oak_log",
                    "goal": "Harvest one oak log.",
                    "success_criteria": [{"type": "inventory_delta_contains", "item": "oak_log"}],
                    "runtime": {
                        "host": "localhost",
                        "port": 52329,
                        "username": "HarnessTrainer1",
                    },
                    "training": {
                        "job_id": "week10_live_test",
                        "mode": "parallel_single_agent_live",
                        "worker_id": "worker-1",
                        "memory_namespace": "week10_live_test:minedojo_harvest_oak_log:attempt-1",
                    },
                },
                started_at=started_at,
            )
        )
        session.add(
            StepRecord(
                run_id="run_dashboard",
                step_index=0,
                observation={"inventory": []},
                action=action.model_dump(mode="json"),
                action_result={"ok": True},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="run_started",
                payload={"task_id": "minedojo_harvest_oak_log"},
                agent_id="agent-1",
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="environment_reset",
                payload={"reset_policy": {"clear_inventory": {"enabled": True, "verified": True}}},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="observation",
                payload={"step_index": 0, "observation": {"inventory": []}},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="context_built",
                payload={
                    "step_index": 0,
                    "resolved_terms": ["oak_log"],
                    "retrieved_docs": ["minecraft.blocks.oak_log"],
                    "retrieved_skills": [{"name": "collect_wood", "version": "0.1.0"}],
                },
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="model_action",
                payload={
                    "step_index": 0,
                    "raw_content": '{"type":"dig_block_at"}',
                    "action": action.model_dump(mode="json"),
                    "usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
                },
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="action_result",
                payload={
                    "step_index": 0,
                    "action": action.model_dump(mode="json"),
                    "result": {"ok": True},
                },
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="runtime_error",
                payload={"step_index": 1, "error_type": "worker_timeout", "message": "timeout"},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_dashboard",
                event_type="verifier_result",
                payload={"success": True, "reason": "Inventory delta is +1 oak_log."},
            )
        )
        session.add(
            ModelCallRecord(
                run_id="run_dashboard",
                step_index=0,
                raw_content='{"type":"dig_block_at"}',
                action=action.model_dump(mode="json"),
                usage={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
                raw_response={"id": "call_test"},
            )
        )
        session.add(
            RuntimeErrorRecord(
                run_id="run_dashboard",
                step_index=1,
                error_type="worker_timeout",
                message="timeout",
                payload={"timeout_sec": 10},
            )
        )
        session.add(
            SkillRecord(
                name=skill.name,
                version=skill.version,
                status=skill.status.value,
                spec=skill.model_dump(mode="json"),
                source_run_id="run_dashboard",
            )
        )
        session.commit()


def _seed_completed_failed_run(session_factory: sessionmaker[Session]) -> None:
    """Insert one lifecycle-completed run whose task verifier failed."""

    started_at = datetime(2026, 6, 24, 13, 0, tzinfo=UTC)
    finished_at = datetime(2026, 6, 24, 13, 5, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            RunRecord(
                id="run_completed_failed",
                task_id="minedojo_harvest_oak_log",
                status="completed",
                task_spec={
                    "task_id": "minedojo_harvest_oak_log",
                    "goal": "Harvest one oak log.",
                    "runtime": {
                        "host": "localhost",
                        "port": 52329,
                        "username": "HarnessTrainerFailed",
                    },
                },
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_completed_failed",
                event_type="run_started",
                payload={"task_id": "minedojo_harvest_oak_log"},
            )
        )
        session.add(
            TrajectoryEventRecord(
                run_id="run_completed_failed",
                event_type="verifier_result",
                payload={
                    "success": False,
                    "verifier": {
                        "success": False,
                        "reason": "Inventory delta is +0 oak_log.",
                    },
                },
            )
        )
        session.commit()


def _first_event_id(session_factory: sessionmaker[Session], run_id: str) -> int:
    """Return the first trajectory event id for a seeded run."""

    with session_factory() as session:
        event_id = session.scalar(
            select(TrajectoryEventRecord.id)
            .where(TrajectoryEventRecord.run_id == run_id)
            .order_by(TrajectoryEventRecord.id)
            .limit(1)
        )
    assert event_id is not None
    return event_id
