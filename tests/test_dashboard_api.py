"""End-to-end API tests for the dashboard endpoints."""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient


def test_weekly_report_empty_baseline(client: TestClient) -> None:
    resp = client.get("/api/v1/dashboard/weekly-report/child-001")
    assert resp.status_code == 200
    data = resp.json()
    assert "headline" in data
    assert "discoveries" in data
    assert "action_suggestions" in data


def test_weekly_report_with_data(client: TestClient) -> None:
    """Add some baseline records, then check the report."""
    today = datetime.utcnow()
    for i in range(7):
        d = (today - timedelta(days=i + 1)).date().isoformat()
        client.post(
            "/api/v1/dashboard/baseline/add",
            json={"date": d, "valence": 0.2, "intensity": 3},
        )

    resp = client.get("/api/v1/dashboard/weekly-report/child-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["child_id"] == "child-001"
    assert "🌟" in data["headline"] or "😊" in data["headline"] or "🌈" in data["headline"]


def test_interest_graph_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/dashboard/interest/child-001")
    assert resp.status_code == 200
    data = resp.json()
    assert "top_topics" in data
    assert "growing_topics" in data


def test_interest_graph_with_queries(client: TestClient) -> None:
    for q in ["恐龙", "恐龙", "月亮", "为什么天是蓝的"]:
        client.post(
            "/api/v1/dashboard/events/knowledge",
            json={"child_id": "child-001", "question": q, "topic": q},
        )
    resp = client.get("/api/v1/dashboard/interest/child-001")
    data = resp.json()
    assert len(data["top_topics"]) >= 1
    # 恐龙 was queried twice → should be top
    assert data["top_topics"][0]["name"] == "恐龙"


def test_learning_whitepaper(client: TestClient) -> None:
    client.post(
        "/api/v1/dashboard/events/knowledge",
        json={"child_id": "child-001", "question": "恐龙", "topic": "恐龙"},
    )
    client.post(
        "/api/v1/dashboard/events/tutoring",
        json={"child_id": "child-001", "problem": "3+5", "solved": True},
    )
    resp = client.get("/api/v1/dashboard/learning/child-001")
    assert resp.status_code == 200
    data = resp.json()
    assert "恐龙" in data["new_knowledge_explored"]
    # Just check the structure works (specific counts may vary due to test order)
    assert data["total_questions_asked"] >= 1
    assert data["total_problems_solved"] >= 1


def test_record_safety_valve_alert(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/dashboard/alerts/record/safety-valve",
        json={"child_id": "child-001", "problem_summary": "数学应用题"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["severity"] == "info"
    assert "数学应用题" in data["problem_summary"]


def test_record_high_risk_alert(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/dashboard/alerts/record/high-risk",
        json={"child_id": "child-001", "original_text": "不想活"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["severity"] == "urgent"
    assert "不想活" in data["original_text"]


def test_get_alerts(client: TestClient) -> None:
    client.post(
        "/api/v1/dashboard/alerts/record/safety-valve",
        json={"child_id": "child-001", "problem_summary": "数学题"},
    )
    client.post(
        "/api/v1/dashboard/alerts/record/high-risk",
        json={"child_id": "child-001", "original_text": "不要活了"},
    )
    resp = client.get("/api/v1/dashboard/alerts/child-001")
    data = resp.json()
    assert len(data["safety_valve"]) >= 1
    assert len(data["high_risk"]) >= 1


def test_mark_alert_delivered(client: TestClient) -> None:
    # Record an alert
    resp = client.post(
        "/api/v1/dashboard/alerts/record/high-risk",
        json={"child_id": "child-001", "original_text": "help"},
    )
    alert_id = resp.json()["id"]
    # Mark as delivered
    resp = client.post(f"/api/v1/dashboard/alerts/{alert_id}/delivered")
    assert resp.status_code == 200
    assert resp.json()["delivered"] is True
    # Should not appear in pending now
    resp = client.get("/api/v1/dashboard/alerts/child-001")
    pending = resp.json()["high_risk"]
    assert all(a["id"] != alert_id for a in pending)


def test_alerts_per_child_isolated(client: TestClient) -> None:
    client.post(
        "/api/v1/dashboard/alerts/record/safety-valve",
        json={"child_id": "child-A", "problem_summary": "math"},
    )
    client.post(
        "/api/v1/dashboard/alerts/record/safety-valve",
        json={"child_id": "child-B", "problem_summary": "english"},
    )
    resp_a = client.get("/api/v1/dashboard/alerts/child-A")
    resp_b = client.get("/api/v1/dashboard/alerts/child-B")
    assert resp_a.json()["safety_valve"][0]["problem_summary"] == "math"
    assert resp_b.json()["safety_valve"][0]["problem_summary"] == "english"
