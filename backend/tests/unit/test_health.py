from fastapi.testclient import TestClient

from mc_agent_harness.main import create_app


def test_health() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

