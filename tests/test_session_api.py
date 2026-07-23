"""End-to-end API tests for the session endpoints."""

from fastapi.testclient import TestClient


def _start_session(client: TestClient, child_id: str = "test-c1") -> None:
    client.post("/api/v1/session/start", json={"child_id": child_id})


def test_start_session(client: TestClient) -> None:
    resp = client.post("/api/v1/session/start", json={"child_id": "test-c1"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


def test_record_activity(client: TestClient) -> None:
    _start_session(client)
    resp = client.post(
        "/api/v1/session/activity",
        json={"child_id": "test-c1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_get_stats(client: TestClient) -> None:
    _start_session(client)
    resp = client.get("/api/v1/session/stats/test-c1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_started"] is True
    assert data["daily_limit_minutes"] == 60
    assert "elapsed_minutes" in data
    assert "remaining_minutes" in data


def test_should_warn_initial(client: TestClient) -> None:
    _start_session(client)
    resp = client.get("/api/v1/session/should-warn/test-c1")
    data = resp.json()
    # At start, no warning
    assert data["should_warn"] is False


def test_is_exhausted_initial(client: TestClient) -> None:
    _start_session(client)
    resp = client.get("/api/v1/session/is-exhausted/test-c1")
    data = resp.json()
    assert data["is_exhausted"] is False


def test_is_idle_initial(client: TestClient) -> None:
    _start_session(client)
    resp = client.get("/api/v1/session/is-idle/test-c1")
    data = resp.json()
    assert data["is_idle_timeout"] is False


def test_end_session(client: TestClient) -> None:
    _start_session(client)
    resp = client.post(
        "/api/v1/session/end",
        json={"child_id": "test-c1", "reason": "child_bye"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "stats" in data
    assert data["stats"]["exit_reason"] == "child_bye"


def test_end_session_no_active(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/session/end",
        json={"child_id": "never-started", "reason": "child_bye"},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_end_session_invalid_reason_defaults_to_child_bye(client: TestClient) -> None:
    _start_session(client)
    resp = client.post(
        "/api/v1/session/end",
        json={"child_id": "test-c1", "reason": "invalid_reason"},
    )
    data = resp.json()
    assert data["stats"]["exit_reason"] == "child_bye"


def test_parent_pause_request(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pause_queued"


def test_parent_has_pause(client: TestClient) -> None:
    # Use a unique child_id per test invocation
    import uuid
    child_id = f"test-pause-{uuid.uuid4().hex[:16]}"
    # Initially no pause
    resp = client.get(f"/api/v1/session/parent/has-pause/{child_id}")
    assert resp.json()["has_pending_pause"] is False
    # Add pause
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": child_id, "parent_id": "p1"},
    )
    # Now has pause
    resp = client.get(f"/api/v1/session/parent/has-pause/{child_id}")
    assert resp.json()["has_pending_pause"] is True


def test_parent_consume_pause(client: TestClient) -> None:
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    resp = client.post(
        "/api/v1/session/parent/consume-pause",
        json={"child_id": "test-c1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "exit_message" in data
    # Pause should now be consumed
    assert data["exit_message"]["show_summary"] is False


def test_parent_consume_pause_no_pending(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/session/parent/consume-pause",
        json={"child_id": "never-paused"},
    )
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_parent_end_session(client: TestClient) -> None:
    _start_session(client)
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    resp = client.post(
        "/api/v1/session/parent/end-session",
        json={
            "child_id": "test-c1",
            "topics_visited": ["恐龙", "月亮"],
        },
    )
    data = resp.json()
    assert "stats" in data
    assert "exit_message" in data
    assert data["stats"]["exit_reason"] == "parent_pause"


def test_parent_pause_message_does_not_reveal_intervention(client: TestClient) -> None:
    """PRD #33: child must not know parent intervened."""
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    resp = client.post(
        "/api/v1/session/parent/consume-pause",
        json={"child_id": "test-c1"},
    )
    msg = resp.json()["exit_message"]["text"]
    for word in ["parent", "妈妈", "爸爸"]:
        assert word not in msg.lower()


def test_parent_paused_check(client: TestClient) -> None:
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    client.post(
        "/api/v1/session/parent/consume-pause",
        json={"child_id": "test-c1"},
    )
    resp = client.get("/api/v1/session/parent/paused/test-c1")
    assert resp.json()["is_paused"] is True


def test_parent_clear_pause(client: TestClient) -> None:
    client.post(
        "/api/v1/session/parent/pause",
        json={"child_id": "test-c1", "parent_id": "p1"},
    )
    client.post(
        "/api/v1/session/parent/clear-pause",
        json={"child_id": "test-c1"},
    )
    resp = client.get("/api/v1/session/parent/paused/test-c1")
    assert resp.json()["is_paused"] is False
