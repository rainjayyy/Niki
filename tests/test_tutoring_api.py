"""End-to-end API tests for the tutoring endpoints."""

from fastapi.testclient import TestClient


def test_ask_pure_calculation(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "3 + 5 = ?", "child_age": 7, "subtype": "pure_calculation"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "text" in data
    assert data["intent"] == "tutoring"
    assert data["metadata"]["round"] == 1
    assert data["metadata"]["safety_valve_triggered"] is False


def test_ask_word_problem(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "小明有3个苹果", "child_age": 7, "subtype": "word_problem"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["subtype"] == "word_problem"


def test_ask_visual_problem(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "看图回答", "child_age": 7, "subtype": "visual_problem"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["subtype"] == "visual_problem"


def test_ask_with_negative_emotion_triggers_pre_step(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "我不会，太难了", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["pre_step_applied"] is True
    assert data["emotion"] == "negative"


def test_ask_with_positive_emotion_no_pre_step(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "太简单啦", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["pre_step_applied"] is False
    assert data["emotion"] == "positive"


def test_ask_empty_problem(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "再说一次" in data["text"] or "没看清" in data["text"]


def test_ask_metadata_includes_difficulty(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "3 + 5", "child_age": 7, "subtype": "word_problem"},
    )
    data = resp.json()
    assert "difficulty" in data["metadata"]
    assert data["metadata"]["difficulty"] in ("easy", "medium", "hard")


def test_ask_with_age_3_to_5(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "3 + 5", "child_age": 4},
    )
    assert resp.status_code == 200


def test_ask_with_age_9_to_12(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "3 + 5", "child_age": 11},
    )
    assert resp.status_code == 200


def test_strategies_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/tutoring/strategies")
    assert resp.status_code == 200
    data = resp.json()
    assert "rounds" in data
    assert data["max_attempts"] == 3
    # 4 rounds
    assert len(data["rounds"]) == 4
    # Each round has templates for all 4 subtypes
    for round in data["rounds"]:
        assert "round" in round
        assert "templates" in round
        assert len(round["templates"]) == 4


def test_difficulty_endpoint(client: TestClient) -> None:
    resp = client.get("/api/v1/tutoring/difficulty")
    assert resp.status_code == 200
    data = resp.json()
    assert "subtype_discounts" in data
    assert "pure_calculation" in data["subtype_discounts"]
    assert "thresholds" in data


def test_ask_never_gives_direct_answer(client: TestClient) -> None:
    """Even on a simple 3+5 problem, the agent doesn't reveal the answer."""
    resp = client.post(
        "/api/v1/tutoring/ask",
        json={"problem": "3 + 5 = ?", "child_age": 7, "subtype": "pure_calculation"},
    )
    data = resp.json()
    # The first response should be a Socratic question, not the answer
    assert "答案是" not in data["text"]
