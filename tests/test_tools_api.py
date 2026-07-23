"""End-to-end API tests for the tool permission endpoints."""


from fastapi.testclient import TestClient


def test_get_matrix(client: TestClient) -> None:
    resp = client.get("/api/v1/tools/matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert "matrix" in data
    assert "search_api" in data["matrix"]
    assert "encyclopedia_agent" in data["matrix"]["search_api"]


def test_get_allowed_tools_for_encyclopedia(client: TestClient) -> None:
    resp = client.get("/api/v1/tools/allowed/encyclopedia_agent")
    assert resp.status_code == 200
    data = resp.json()
    assert "search_api" in data["tools"]
    assert "time_query" in data["tools"]


def test_get_allowed_tools_for_tutoring(client: TestClient) -> None:
    resp = client.get("/api/v1/tools/allowed/tutoring_agent")
    assert resp.status_code == 200
    data = resp.json()
    # Tutoring has NO external tools
    assert "search_api" not in data["tools"]
    assert "weather_query" not in data["tools"]
    # But has time_query + image_recognition
    assert "time_query" in data["tools"]
    assert "image_recognition" in data["tools"]


def test_check_access_allowed(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/check",
        json={"agent": "encyclopedia_agent", "tool": "search_api"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["allowed"] is True


def test_check_access_denied(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/check",
        json={"agent": "tutoring_agent", "tool": "search_api"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["allowed"] is False
    assert "NOT in allow-list" in data["reason"]


def test_check_access_unknown_tool(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/check",
        json={"agent": "encyclopedia_agent", "tool": "fake_tool"},
    )
    data = resp.json()
    assert data["allowed"] is False
    assert "unknown tool" in data["reason"]


def test_calculator_checked_all_agents_denied(client: TestClient) -> None:
    """PRD: calculator was globally removed."""
    for agent in ("encyclopedia_agent", "tutoring_agent", "emotional_agent"):
        resp = client.post(
            "/api/v1/tools/check",
            json={"agent": agent, "tool": "calculator"},
        )
        assert resp.json()["allowed"] is False


def test_revoke_tool(client: TestClient) -> None:
    # Use a unique tool name to not interfere with other tests
    resp = client.post(
        "/api/v1/tools/revoke",
        json={"tool": "search_api"},
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True
    # Now search_api should be denied to all
    resp = client.post(
        "/api/v1/tools/check",
        json={"agent": "encyclopedia_agent", "tool": "search_api"},
    )
    assert resp.json()["allowed"] is False


def test_revoke_unknown_tool(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/revoke",
        json={"tool": "fake_tool"},
    )
    data = resp.json()
    assert "unknown tool" in data["error"]


def test_grant_tool(client: TestClient) -> None:
    # Grant a tool to a new agent
    resp = client.post(
        "/api/v1/tools/grant",
        json={"tool": "search_api", "agents": ["custom_agent"]},
    )
    assert resp.status_code == 200
    # Now custom_agent can use it
    resp = client.post(
        "/api/v1/tools/check",
        json={"agent": "custom_agent", "tool": "search_api"},
    )
    assert resp.json()["allowed"] is True


def test_grant_unknown_tool(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/grant",
        json={"tool": "fake_tool", "agents": ["x"]},
    )
    data = resp.json()
    assert "unknown tool" in data["error"]


def test_grant_non_list_agents(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tools/grant",
        json={"tool": "search_api", "agents": "not-a-list"},
    )
    data = resp.json()
    assert "list" in data["error"]


def test_audit_log(client: TestClient) -> None:
    # Make some checks
    client.post(
        "/api/v1/tools/check",
        json={"agent": "encyclopedia_agent", "tool": "search_api"},
    )
    client.post(
        "/api/v1/tools/check",
        json={"agent": "tutoring_agent", "tool": "search_api"},
    )
    resp = client.get("/api/v1/tools/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_checks"] >= 2
    assert data["denied_count"] >= 1
    assert len(data["recent"]) >= 2
