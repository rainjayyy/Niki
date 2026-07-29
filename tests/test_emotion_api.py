"""End-to-end API tests for the emotion endpoints."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_express_sad(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我今天很难过", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["emotion"] == "sad"


def test_express_happy(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我今天好开心", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["emotion"] == "happy"


def test_express_treehole_activated(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我有一个秘密", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["treehole_activated"] is True


def test_express_empty_text(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "说" in data["text"] or "听" in data["text"]


def test_baseline_initial_calibration(client: TestClient) -> None:
    resp = client.get("/api/v1/emotion/baseline")
    assert resp.status_code == 200
    data = resp.json()
    # Either still in calibration (early) OR has some records (after earlier tests)
    # — the test is just checking the endpoint is responsive
    assert "in_calibration" in data
    assert "sample_count" in data
    assert data["z_score_threshold"] == 1.5


def test_baseline_after_record(client: TestClient) -> None:
    # Add a record dated 2 days ago (so it's in the 7-day window)
    today = datetime.now(timezone.utc).date()
    past = (today - timedelta(days=2)).isoformat()
    resp = client.post(
        "/api/v1/emotion/baseline/add",
        json={"date": past, "valence": 0.0, "intensity": 3},
    )
    assert resp.status_code == 200

    # Check baseline
    resp = client.get("/api/v1/emotion/baseline")
    data = resp.json()
    assert data["sample_count"] >= 1


def test_proactive_care_no_care_in_calibration(client: TestClient) -> None:
    resp = client.post("/api/v1/emotion/proactive-care")
    assert resp.status_code == 200
    data = resp.json()
    # In calibration or no anomaly
    assert data["should_care"] is False


def test_good_night(client: TestClient) -> None:
    resp = client.get("/api/v1/emotion/good-night")
    assert resp.status_code == 200
    data = resp.json()
    assert "晚安" in data["text"]


def test_express_with_age_3_to_5(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我难过", "child_age": 4},
    )
    assert resp.status_code == 200


def test_express_with_age_9_to_12(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我难过", "child_age": 11},
    )
    assert resp.status_code == 200


def test_express_intensity_high_for_emphasis(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我非常非常难过！", "child_age": 7},
    )
    data = resp.json()
    assert data["metadata"]["intensity"] >= 4


def test_express_metadata_includes_anomaly(client: TestClient) -> None:
    """After several expressions, the metadata should include anomaly info."""
    for _ in range(5):
        client.post(
            "/api/v1/emotion/express",
            json={"text": "我难过", "child_age": 7},
        )
    resp = client.post(
        "/api/v1/emotion/express",
        json={"text": "我难过", "child_age": 7},
    )
    data = resp.json()
    # Anomaly key may be None (in calibration) or a dict
    assert "anomaly" in data["metadata"]
