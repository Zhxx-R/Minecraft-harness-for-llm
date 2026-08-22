from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mc_agent_harness.api.routes.dashboard import get_session
from mc_agent_harness.core.config import settings
from mc_agent_harness.db.models import (
    Base,
    HumanReviewRecord,
    RunRecord,
    TrajectoryEventRecord,
)
from mc_agent_harness.main import create_app


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an isolated SQL store for human-review API tests."""

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
    """Bind the dashboard API to the isolated review database."""

    app = create_app()

    def override_session() -> Iterator[Session]:
        """Yield one test session for each request."""

        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_human_review_serves_media_and_records_authoritative_decision(
    client: TestClient,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review API hides paths, streams trusted evidence, and appends an audit decision."""

    monkeypatch.setattr(settings, "artifact_root", str(tmp_path))
    video = tmp_path / "agent_pov.mp4"
    image = tmp_path / "final.jpg"
    video.write_bytes(b"video-evidence")
    image.write_bytes(b"image-evidence")
    with session_factory() as session:
        session.add(
            RunRecord(
                id="review-run",
                task_id="creative:small_house",
                status="awaiting_human_review",
                task_spec={"category": "creative", "agent_id": "creative-agent"},
                started_at=datetime.now(tz=UTC),
                finished_at=datetime.now(tz=UTC),
            )
        )
        session.add(
            HumanReviewRecord(
                run_id="review-run",
                task_id="creative:small_house",
                task_name="Build a small house",
                status="awaiting_review",
                submission_summary="The shelter has four walls, a roof, and a doorway.",
                evidence={
                    "source": {"type": "video", "path": str(video)},
                    "final_frame": {"path": str(image), "sequence": 31},
                    "mineclip": {"score": 0.62, "status": "completed"},
                },
            )
        )
        session.commit()

    listed = client.get("/api/human-reviews")
    assert listed.status_code == 200
    review = listed.json()[0]
    assert review["task_name"] == "Build a small house"
    assert review["media"]["video_available"] is True
    assert review["media"]["image_available"] is True
    assert "path" not in str(review)
    assert client.get(review["media"]["video_url"]).content == b"video-evidence"
    assert client.get(review["media"]["image_url"]).content == b"image-evidence"

    decided = client.post(
        "/api/human-reviews/review-run/decision",
        json={
            "decision": "approved",
            "reviewer_id": "reviewer-1",
            "notes": "Goal visibly satisfied.",
            "reason_codes": ["goal satisfied", "goal satisfied"],
            "expected_version": 1,
        },
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert decided.json()["version"] == 2
    assert decided.json()["reason_codes"] == ["goal_satisfied"]

    stale = client.post(
        "/api/human-reviews/review-run/decision",
        json={"decision": "rejected", "expected_version": 1},
    )
    assert stale.status_code == 409

    with session_factory() as session:
        run = session.get(RunRecord, "review-run")
        event = session.scalar(
            select(TrajectoryEventRecord).where(
                TrajectoryEventRecord.run_id == "review-run",
                TrajectoryEventRecord.event_type == "human_review_decided",
            )
        )
    assert run is not None and run.status == "succeeded"
    assert event is not None
    assert event.payload["authority"] == "human"
    assert event.payload["decision"] == "approved"
