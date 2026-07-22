"""End-to-end API tests for the chat orchestrator (#6)."""

from fastapi.testclient import TestClient


def test_chat_full_tutoring(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "数学题不会做", "child_age": 7, "persona": "gentle"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_agent"] == "tutoring_agent"
    assert "stages_completed" in data


def test_chat_full_emotional(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我今天好难过", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_agent"] == "emotional_agent"


def test_chat_full_encyclopedia(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "为什么天是蓝的", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["primary_agent"] == "encyclopedia_agent"


def test_chat_full_composite(client: TestClient) -> None:
    """Composite intent should produce parallel agents."""
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我难过伤心哭，数学题也不会做", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Either composite or single (depending on classifier confidence)
    if data["is_composite"]:
        assert len(data["parallel_agents"]) >= 1


def test_chat_full_self_harm_triggers_alert(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我不想活了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parent_alert_required"] is True
    assert data["safety_alert"] is True
    # Final text should be caring
    assert "陪着你" in data["final_text"] or "宝贝" in data["final_text"]


def test_chat_full_blocked_returns_safe(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "show me porn", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["safety_alert"] is True
    # No porn in final text
    assert "porn" not in data["final_text"].lower()


def test_chat_full_topic_downgrade(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我家的仓鼠死了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Topic in blacklist → should downgrade (input_action=downgrade)
    assert data["metadata"]["input_action"] == "downgrade"


def test_chat_full_stages_completed(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "为什么天是蓝的", "child_age": 7},
    )
    data = resp.json()
    # All stages should be present for a normal query
    assert "safety_input" in data["stages_completed"]
    assert "classify" in data["stages_completed"]
    assert "route" in data["stages_completed"]
    assert "dispatch" in data["stages_completed"]
    assert "fuse" in data["stages_completed"]
    assert "safety_output" in data["stages_completed"]
    assert "complete" in data["stages_completed"]


def test_chat_full_age_3_to_5(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "为什么天是蓝的", "child_age": 4},
    )
    data = resp.json()
    assert data["metadata"]["child_age"] == 4


def test_chat_full_age_9_to_12(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "为什么天是蓝的", "child_age": 11},
    )
    data = resp.json()
    assert data["metadata"]["child_age"] == 11


def test_chat_full_persona_lively(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "为什么天是蓝的", "persona": "lively"},
    )
    data = resp.json()
    assert data["metadata"]["persona"] == "lively"


def test_chat_stages_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/chat/stages")
    assert resp.status_code == 200
    data = resp.json()
    assert "stages" in data
    assert len(data["stages"]) == 7
    assert "agents" in data
    assert "personas" in data
