"""Tests for the health check endpoint."""

from fastapi.testclient import TestClient


def test_health_live(client: TestClient) -> None:
    """The liveness probe should always return 200."""
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "alive"


def test_health_ready(client: TestClient) -> None:
    """The readiness probe returns ready/not_ready."""
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "ready" in data


def test_health_main_endpoint(client: TestClient) -> None:
    """The main health endpoint returns all component statuses."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded", "unhealthy")
    assert "version" in data
    assert "components" in data
    assert len(data["components"]) == 6  # redis, chroma, neo4j, pg, deepseek, doubao


def test_health_component_structure(client: TestClient) -> None:
    """Each component in health response should have required fields."""
    response = client.get("/api/v1/health")
    data = response.json()

    component_names = {c["component"] for c in data["components"]}
    assert "redis" in component_names
    assert "postgresql" in component_names

    for component in data["components"]:
        assert "component" in component
        assert "status" in component
        assert component["status"] in ("healthy", "degraded", "unhealthy")
