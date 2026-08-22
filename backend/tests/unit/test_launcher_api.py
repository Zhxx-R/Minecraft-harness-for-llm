from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from mc_agent_harness.api.routes.launcher import (
    get_quick_start_service,
    get_task_catalog,
)
from mc_agent_harness.launcher.catalog import ExecutableTaskCatalog
from mc_agent_harness.main import create_app


class StubLauncher:
    """Minimal launcher dependency used to test API authorization and safe config."""

    def public_config(self) -> dict[str, object]:
        """Return deterministic secret-free frontend defaults."""

        return {
            "server_host": "127.0.0.1",
            "server_port": 25565,
            "rcon_host": "127.0.0.1",
            "rcon_port": 25575,
            "default_client_player": "viewer",
            "recording_window_title": "Minecraft",
            "rcon_password_configured": True,
            "model_configured": True,
            "active_job_id": None,
        }

    def list_jobs(self) -> list[object]:
        """Return no process records for catalog-only route tests."""

        return []

    async def start_job(self, task, configuration):
        """Return one waiting workflow without creating local processes."""

        return (
            StubPayload(
                {
                    "job_id": "launch-test",
                    "task_id": task.task_id,
                    "task_kind": task.kind,
                    "task_goal": str(task.manifest["goal"]),
                    "view_mode": configuration.view_mode,
                    "client_player": configuration.client_player,
                    "server_host": configuration.server_host,
                    "server_port": configuration.server_port,
                    "status": "waiting_for_client",
                    "status_detail": "Join the server before recording.",
                    "server_started_by_job": True,
                }
            ),
            StubPayload(
                {
                    "launchable": True,
                    "runtime_ready": False,
                    "checks": [
                        {
                            "name": "minecraft_client",
                            "ok": False,
                            "state": "pending",
                            "detail": "Waiting for viewer.",
                        }
                    ],
                }
            ),
        )


class StubPayload:
    """Small JSON adapter used by launcher route dependency tests."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def to_json(self) -> dict[str, object]:
        return self.payload


def test_launcher_catalog_and_random_endpoints(tmp_path: Path) -> None:
    """Quick-start endpoints paginate and draw only from trusted snapshots."""

    app = create_app()
    app.dependency_overrides[get_task_catalog] = lambda: _catalog(tmp_path)
    app.dependency_overrides[get_quick_start_service] = StubLauncher
    client = TestClient(app)

    listed = client.get("/api/launcher/tasks?kind=programmatic&limit=10")
    random_task = client.get("/api/launcher/tasks/random?kind=creative")
    detail = client.get("/api/launcher/tasks/harvest_oak_log")

    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["task_id"] == "harvest_oak_log"
    assert random_task.status_code == 200
    assert random_task.json()["task_id"] == "creative:1"
    assert detail.status_code == 200
    assert detail.json()["verifier"]["type"] == "inventory_delta"


def test_launcher_state_changes_require_explicit_local_control_header(
    tmp_path: Path,
) -> None:
    """A browser cannot start a child process without the dashboard control header."""

    app = create_app()
    app.dependency_overrides[get_task_catalog] = lambda: _catalog(tmp_path)
    app.dependency_overrides[get_quick_start_service] = StubLauncher
    client = TestClient(app)

    response = client.post(
        "/api/launcher/jobs",
        json={
            "task_id": "harvest_oak_log",
            "view_mode": "agent",
            "client_player": "viewer",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "local_control_header_required"


def test_launcher_can_return_waiting_for_client_workflow(tmp_path: Path) -> None:
    """Authorized Start Task returns immediately while the client is still joining."""

    app = create_app()
    app.dependency_overrides[get_task_catalog] = lambda: _catalog(tmp_path)
    app.dependency_overrides[get_quick_start_service] = StubLauncher
    client = TestClient(app)

    response = client.post(
        "/api/launcher/jobs",
        headers={"X-Harness-Control": "local-dashboard-v1"},
        json={
            "task_id": "creative:1",
            "view_mode": "agent",
            "client_player": "viewer",
        },
    )

    assert response.status_code == 200
    assert response.json()["job"]["status"] == "waiting_for_client"
    assert response.json()["job"]["server_host"] == "127.0.0.1"
    assert response.json()["preflight"]["runtime_ready"] is False


def _catalog(tmp_path: Path) -> ExecutableTaskCatalog:
    """Create the two fixed snapshots expected by the route dependency."""

    programmatic_path = tmp_path / "programmatic.jsonl"
    creative_path = tmp_path / "creative.jsonl"
    programmatic_path.write_text(
        json.dumps(
            {
                "task_id": "harvest_oak_log",
                "category": "harvest",
                "goal": "Collect one oak log",
                "verifier": {"type": "inventory_delta"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    creative_path.write_text(
        json.dumps(
            {
                "task_id": "creative:1",
                "category": "creative",
                "goal": "Build a shelter",
                "verifier": {"type": "human_review"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return ExecutableTaskCatalog(programmatic_path, creative_path)
