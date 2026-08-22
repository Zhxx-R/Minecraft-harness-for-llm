from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from mc_agent_harness.api.routes.configuration import (
    get_configuration_session,
    get_prompt_configuration_service,
)
from mc_agent_harness.configuration import PromptConfigurationService
from mc_agent_harness.db.models import Base, KnowledgeChunkRecord
from mc_agent_harness.main import create_app


_CONTROL_HEADERS = {"X-Harness-Control": "local-dashboard-v1"}


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    """Create one isolated SQL store for configuration API tests."""

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
    """Bind configuration dependencies to the isolated test database."""

    app = create_app()

    def override_session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    def override_service() -> PromptConfigurationService:
        return PromptConfigurationService(session_factory)

    app.dependency_overrides[get_configuration_session] = override_session
    app.dependency_overrides[get_prompt_configuration_service] = override_service
    return TestClient(app)


def test_knowledge_chunks_support_facets_pagination_and_kind_filter(
    client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    """Knowledge browsing returns stable facets and filtered pages."""

    with session_factory() as session:
        session.add_all(
            [
                KnowledgeChunkRecord(
                    id="doc:sheep",
                    source="managed.docs",
                    title="Sheep wool",
                    content="Sheep wool color is decoded from metadata.",
                    tags=["sheep", "wool"],
                    chunk_metadata={"kind": "document"},
                    enabled=True,
                ),
                KnowledgeChunkRecord(
                    id="term:sheep",
                    source="generated.terms",
                    title="Sheep",
                    content="Canonical passive entity.",
                    tags=["entity"],
                    chunk_metadata={"kind": "entity"},
                    enabled=True,
                ),
                KnowledgeChunkRecord(
                    id="doc:archived",
                    source="managed.docs",
                    title="Old sheep note",
                    content="No longer active.",
                    tags=[],
                    chunk_metadata={"kind": "document"},
                    enabled=False,
                ),
            ]
        )
        session.commit()

    response = client.get(
        "/api/knowledge-chunks",
        params={"q": "sheep", "enabled": "true", "offset": 0, "limit": 1},
    )

    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert page["sources"] == {"generated.terms": 1, "managed.docs": 1}
    assert page["kinds"] == {"document": 1, "entity": 1}
    assert page["items"][0]["has_embedding"] is False

    documents = client.get(
        "/api/knowledge-chunks",
        params={"q": "sheep", "kind": "document"},
    )
    assert documents.status_code == 200
    assert {item["id"] for item in documents.json()["items"]} == {
        "doc:sheep",
        "doc:archived",
    }


def test_knowledge_chunk_crud_archive_and_version_conflicts(client: TestClient) -> None:
    """Managed chunks require local control and reject stale editors."""

    create_payload = {
        "id": "managed:white-wool",
        "source": "managed.console",
        "title": "White wool",
        "content": "Prefer sheep whose decoded wool color is white.",
        "tags": ["wool", "wool"],
        "metadata": {"kind": "document"},
        "enabled": True,
    }
    forbidden = client.post("/api/knowledge-chunks", json=create_payload)
    assert forbidden.status_code == 403

    created = client.post(
        "/api/knowledge-chunks",
        json=create_payload,
        headers=_CONTROL_HEADERS,
    )
    assert created.status_code == 201
    assert created.json()["version"] == 1
    assert created.json()["tags"] == ["wool"]

    duplicate = client.post(
        "/api/knowledge-chunks",
        json=create_payload,
        headers=_CONTROL_HEADERS,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "knowledge_chunk_exists"

    update_payload = {
        **{key: value for key, value in create_payload.items() if key != "id"},
        "content": "Use decoded white wool metadata and avoid ruled-out entity ids.",
        "expected_version": 1,
    }
    updated = client.patch(
        "/api/knowledge-chunks/managed%3Awhite-wool",
        json=update_payload,
        headers=_CONTROL_HEADERS,
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert "ruled-out" in updated.json()["content"]

    stale = client.patch(
        "/api/knowledge-chunks/managed%3Awhite-wool",
        json=update_payload,
        headers=_CONTROL_HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "stale_knowledge_chunk_version",
        "expected_version": 1,
        "current_version": 2,
    }

    archived = client.post(
        "/api/knowledge-chunks/managed%3Awhite-wool/archive",
        json={"expected_version": 2},
        headers=_CONTROL_HEADERS,
    )
    assert archived.status_code == 200
    assert archived.json()["enabled"] is False
    assert archived.json()["version"] == 3


def test_system_prompt_save_conflict_and_reset(client: TestClient) -> None:
    """The system prompt starts at version zero and resets to its code default."""

    initial = client.get("/api/prompt-configurations")
    assert initial.status_code == 200
    first_bundle = initial.json()
    assert first_bundle["hot_reload"] == {
        "enabled": True,
        "version": 0,
        "persisted": False,
        "effective_source": "code_default",
        "updated_at": None,
    }
    assert first_bundle["system_prompt"]["version"] == 0
    assert first_bundle["system_prompt"]["persisted"] is False

    saved = client.put(
        "/api/prompt-configurations/system",
        json={
            "content": "Use one audited Minecraft action at a time.",
            "enabled": True,
            "expected_version": 0,
        },
        headers=_CONTROL_HEADERS,
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert saved.json()["persisted"] is True
    assert saved.json()["effective_source"] == "database_override"

    stale = client.put(
        "/api/prompt-configurations/system",
        json={
            "content": "stale edit",
            "enabled": True,
            "expected_version": 0,
        },
        headers=_CONTROL_HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == 1

    reset = client.request(
        "DELETE",
        "/api/prompt-configurations/system",
        json={"expected_version": 1},
        headers=_CONTROL_HEADERS,
    )
    assert reset.status_code == 200
    assert reset.json()["version"] == 0
    assert reset.json()["persisted"] is False
    assert reset.json()["effective_source"] == "code_default"

    after_reset = client.get("/api/prompt-configurations").json()
    assert after_reset["system_prompt"]["version"] == 0
    assert after_reset["snapshot_revision"] == first_bundle["snapshot_revision"]


def test_hot_reload_save_conflict_and_reset(client: TestClient) -> None:
    """Prompt hot reload is optional, versioned, and enabled by default."""

    initial = client.get("/api/prompt-configurations").json()
    initial_revision = initial["snapshot_revision"]

    forbidden = client.put(
        "/api/prompt-configurations/hot-reload",
        json={"enabled": False, "expected_version": 0},
    )
    assert forbidden.status_code == 403

    disabled = client.put(
        "/api/prompt-configurations/hot-reload",
        json={"enabled": False, "expected_version": 0},
        headers=_CONTROL_HEADERS,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["version"] == 1
    assert disabled.json()["persisted"] is True
    assert disabled.json()["effective_source"] == "database_override"

    bundle = client.get("/api/prompt-configurations").json()
    assert bundle["hot_reload"] == disabled.json()
    assert bundle["snapshot_revision"] != initial_revision

    stale = client.put(
        "/api/prompt-configurations/hot-reload",
        json={"enabled": True, "expected_version": 0},
        headers=_CONTROL_HEADERS,
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "stale_prompt_configuration_version",
        "kind": "runtime_setting",
        "config_key": "hot_reload",
        "expected_version": 0,
        "current_version": 1,
    }

    reset = client.request(
        "DELETE",
        "/api/prompt-configurations/hot-reload",
        json={"expected_version": 1},
        headers=_CONTROL_HEADERS,
    )
    assert reset.status_code == 200
    assert reset.json() == initial["hot_reload"]

    after_reset = client.get("/api/prompt-configurations").json()
    assert after_reset["snapshot_revision"] == initial_revision


def test_action_prompt_save_reset_and_unknown_action(client: TestClient) -> None:
    """Action overrides are versioned and restricted to implemented primitives."""

    bundle = client.get("/api/prompt-configurations").json()
    follow = next(action for action in bundle["actions"] if action["action_type"] == "follow")
    assert follow["version"] == 0
    assert follow["runtime_supported"] is True
    assert follow["recommended_next_actions"]

    saved = client.put(
        "/api/prompt-configurations/actions/follow",
        json={
            "purpose": "Keep tracking one moving entity.",
            "args": {"entity_id": "numeric entity id"},
            "returns": "Persistent follow state.",
            "when_to_use": "Use before interacting with a mobile entity.",
            "recommended_next_actions": ["use_item: Interact with the followed entity."],
            "prompt_visible": True,
            "expected_version": 0,
        },
        headers=_CONTROL_HEADERS,
    )
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    assert saved.json()["purpose"].startswith("Keep tracking")

    reset = client.request(
        "DELETE",
        "/api/prompt-configurations/actions/follow",
        json={"expected_version": 1},
        headers=_CONTROL_HEADERS,
    )
    assert reset.status_code == 200
    assert reset.json()["version"] == 0
    assert reset.json()["persisted"] is False

    unknown = client.put(
        "/api/prompt-configurations/actions/teleport_anywhere",
        json={
            "purpose": "",
            "args": {},
            "returns": "",
            "when_to_use": "",
            "recommended_next_actions": [],
            "prompt_visible": True,
            "expected_version": 0,
        },
        headers=_CONTROL_HEADERS,
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == {
        "code": "action_not_implemented",
        "action_type": "teleport_anywhere",
    }
