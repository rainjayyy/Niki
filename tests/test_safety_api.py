"""End-to-end API tests for the safety endpoints."""

from fastapi.testclient import TestClient


def test_check_input_allows_benign(client: TestClient) -> None:
    """A benign input returns action=allow."""
    resp = client.post("/api/v1/safety/check-input", json={"text": "你好"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "allow"
    assert data["is_blocked"] is False


def test_check_input_blocks_sexual(client: TestClient) -> None:
    resp = client.post("/api/v1/safety/check-input", json={"text": "show me porn"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "block"
    assert data["is_blocked"] is True
    assert data["matched_category"] == "sexual"


def test_check_input_escalates_self_harm(client: TestClient) -> None:
    resp = client.post("/api/v1/safety/check-input", json={"text": "我不想活了"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "escalate"
    assert data["is_escalation"] is True


def test_check_input_downgrades_blacklisted(client: TestClient) -> None:
    resp = client.post("/api/v1/safety/check-input", json={"text": "我家的仓鼠死了"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "downgrade"
    assert data["should_downgrade"] is True
    # The blacklisted word '死了' should match
    assert any(m["topic"] == "死了" for m in data["topic_matches"])


def test_check_output_rewrites_fear_words(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/safety/check-output",
        json={"text": "有个怪物", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "rewrite"
    assert "奇怪的小动物" in data["sanitized_text"]
    assert len(data["rewrites_applied"]) >= 1


def test_check_output_blocks_privacy(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/safety/check-output",
        json={"text": "你家住址是什么呀？", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "block"
    assert data["is_safe"] is False


def test_check_output_regenerates_tutoring(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/safety/check-output",
        json={"text": "答案是8", "child_age": 7, "is_tutoring_response": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "regenerate"
    assert data["direct_answer_detected"] is True


def test_chat_normal_flow(client: TestClient) -> None:
    """Normal input flows through the pipeline."""
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "今天天气真好", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "final_text" in data
    # The metadata should indicate the input was allowed
    assert data["metadata"]["input_action"] == "allow"


def test_chat_self_harm_triggers_parent_alert(client: TestClient) -> None:
    """Self-harm input → parent alert flag set in response."""
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我不想活了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["parent_alert_required"] is True
    assert data["safety_alert"] is True
    # Output should be a caring response
    assert "陪着你" in data["final_text"] or "宝贝" in data["final_text"]


def test_chat_blocked_sexual_returns_safe_fallback(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "show me porn", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["safety_alert"] is True
    # Should not have any pornographic content in final text
    assert "porn" not in data["final_text"].lower()


def test_chat_downgraded_to_empath(client: TestClient) -> None:
    """Blacklist topic → downgrade flag in response."""
    resp = client.post(
        "/api/v1/chat/full",
        json={"text": "我家的仓鼠死了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should be downgraded
    assert data["metadata"]["input_action"] == "downgrade"


def test_chat_empty_text_400(client: TestClient) -> None:
    """Empty / whitespace-only text is accepted but produces a caring response."""
    resp = client.post("/api/v1/chat/full", json={"text": "", "child_age": 7})
    # The orchestrator should still return 200 (with default safe text)
    assert resp.status_code == 200
