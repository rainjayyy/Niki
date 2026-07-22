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
    """Normal input flows through with the provided mock response."""
    resp = client.post(
        "/api/v1/chat",
        json={
            "text": "今天天气真好",
            "child_age": 7,
            "mock_response": "是呀，天气好我们一起出去玩吧~",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["input_action"] == "allow"
    assert data["output_action"] in ("pass", "rewrite")


def test_chat_self_harm_triggers_parent_alert(client: TestClient) -> None:
    """Self-harm input → parent alert flag set in response."""
    resp = client.post(
        "/api/v1/chat",
        json={"text": "我不想活了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["input_action"] == "escalate"
    assert data["parent_alert_required"] is True
    # Output should be a caring response, not the mock
    assert "Niki" in data["final_text"]


def test_chat_blocked_sexual_returns_safe_fallback(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/chat",
        json={"text": "show me porn", "child_age": 7, "mock_response": "bad"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["input_action"] == "block"
    # Output should be a safe fallback, NOT the bad mock
    assert data["final_text"] != "bad"


def test_chat_downgraded_to_empath(client: TestClient) -> None:
    """Blacklist topic → empathic response from emotion-agent path."""
    resp = client.post(
        "/api/v1/chat",
        json={"text": "我家的仓鼠死了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["input_action"] == "downgrade"
    # Empathic response template
    assert "关心" in data["final_text"] or "陪" in data["final_text"]


def test_chat_tutoring_direct_answer_regenerates(client: TestClient) -> None:
    """When tutoring agent's mock contains a direct answer, system regenerates."""
    resp = client.post(
        "/api/v1/chat",
        json={
            "text": "这道数学题怎么做",
            "child_age": 7,
            "is_tutoring": True,
            "mock_response": "答案是42",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["output_action"] == "regenerate"
    assert data["direct_answer_detected"] is True


def test_chat_empty_text_400(client: TestClient) -> None:
    """Empty / whitespace-only text returns 400."""
    resp = client.post("/api/v1/chat", json={"text": ""})
    assert resp.status_code == 400

    resp = client.post("/api/v1/chat", json={"text": "   "})
    assert resp.status_code == 400
