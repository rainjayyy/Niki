"""End-to-end API tests for the resilience endpoints."""

from fastapi.testclient import TestClient


def test_breaker_stats_initial(client: TestClient) -> None:
    resp = client.get("/api/v1/resilience/breaker/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "closed"
    assert data["total_trips"] == 0
    assert data["window_total"] == 0


def test_breaker_reset(client: TestClient) -> None:
    # Force open
    client.post("/api/v1/resilience/breaker/force-open")
    # Reset
    resp = client.post("/api/v1/resilience/breaker/reset")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "closed"


def test_breaker_force_open(client: TestClient) -> None:
    resp = client.post("/api/v1/resilience/breaker/force-open")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "open"
    assert data["total_trips"] >= 1


def test_degradation_state(client: TestClient) -> None:
    resp = client.get("/api/v1/resilience/degradation")
    assert resp.status_code == 200
    data = resp.json()
    assert "active" in data
    assert "message" in data


def test_degradation_recover(client: TestClient) -> None:
    resp = client.post("/api/v1/resilience/degradation/recover")
    assert resp.status_code == 200
    data = resp.json()
    assert data["active"] is False


def test_resilient_chat_with_messages(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/resilience/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data
    assert "breaker_state" in data
    assert "degradation" in data


def test_resilient_chat_no_messages(client: TestClient) -> None:
    resp = client.post("/api/v1/resilience/chat", json={"messages": []})
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data


def test_resilient_chat_falls_back_on_outage(client: TestClient) -> None:
    """Simulate DeepSeek-V4 outage and verify fallback works."""
    # Simulate outage
    client.post("/api/v1/resilience/simulate-outage")
    # Make a call — should use fallback
    resp = client.post(
        "/api/v1/resilience/chat",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    data = resp.json()
    assert "result" in data
    # Degradation should be active
    assert data["degradation"]["active"] is True
    # Cleanup: restore
    client.post("/api/v1/resilience/simulate-recovery")


def test_simulate_outage_and_recovery(client: TestClient) -> None:
    # Outage
    resp = client.post("/api/v1/resilience/simulate-outage")
    assert resp.status_code == 200
    # Recovery
    resp = client.post("/api/v1/resilience/simulate-recovery")
    assert resp.status_code == 200


def test_degradation_message_is_chinese(client: TestClient) -> None:
    """PRD: message is positive + Chinese, not anxiety-inducing."""
    resp = client.get("/api/v1/resilience/degradation")
    data = resp.json()
    msg = data["message"]
    # Should contain Chinese
    assert any('一' <= c <= '鿿' for c in msg)
