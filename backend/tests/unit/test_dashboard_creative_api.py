from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mc_agent_harness.api.routes.dashboard import get_session
from mc_agent_harness.db.models import Base, CreativeEvaluationRecord, RunRecord
from mc_agent_harness.main import create_app


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create an isolated SQLite database for creative dashboard API tests."""

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
    """Create a dashboard client bound to the isolated test session."""

    app = create_app()

    def override_session() -> Iterator[Session]:
        """Yield a test session to one FastAPI request."""

        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_dashboard_lists_creative_score_trend_and_public_key_frames(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Creative API exposes score evidence while removing private filesystem paths."""

    with session_factory() as session:
        session.add(
            RunRecord(
                id="creative-run",
                task_id="creative:test",
                status="succeeded",
                task_spec={"category": "creative"},
                started_at=datetime.now(UTC),
            )
        )
        session.add(
            CreativeEvaluationRecord(
                run_id="creative-run",
                task_id="creative:test",
                status="completed",
                prompt="Build a stone tower",
                score=0.72,
                score_threshold=0.5,
                success=True,
                scorer="mineclip_official",
                variant="attn",
                calibration_status="calibrated",
                frame_count=32,
                window_count=3,
                result={
                    "score_trend": [
                        {"window_index": 0, "target_probability": 0.6},
                        {"window_index": 1, "target_probability": 0.8},
                    ],
                    "evidence_source": {
                        "type": "video",
                        "path": "/private/runs/agent_pov.mp4",
                    },
                    "final_frame": {
                        "path": "/private/runs/frame_000031.jpg",
                        "sequence": 31,
                    },
                    "key_frames": [
                        {"path": "/private/runs/frame_000010.jpg", "score": 0.8}
                    ],
                },
            )
        )
        session.commit()

    listed = client.get("/api/creative-evaluations")
    detailed = client.get("/api/creative-evaluations/creative-run")

    assert listed.status_code == 200
    assert detailed.status_code == 200
    payload = detailed.json()
    assert payload["score"] == pytest.approx(0.72)
    assert payload["success"] is True
    assert payload["calibration_status"] == "calibrated"
    assert payload["result"]["score_trend"][1]["target_probability"] == pytest.approx(0.8)
    assert "/private/runs" not in str(payload)
    assert payload["result"]["evidence_source"]["filename"] == "agent_pov.mp4"
    assert payload["result"]["final_frame"]["filename"] == "frame_000031.jpg"
    assert payload["result"]["final_frame"]["image_url"].endswith(
        "/human-reviews/creative-run/image"
    )
    key_frame = payload["result"]["key_frames"][0]
    assert "path" not in key_frame
    assert key_frame["filename"] == "frame_000010.jpg"
    assert key_frame["image_url"].endswith("/frames/0")
