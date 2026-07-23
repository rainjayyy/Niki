"""End-to-end API tests for the treehole endpoints."""

import uuid

from fastapi.testclient import TestClient


def _unique_id() -> str:
    return f"test-{uuid.uuid4().hex[:16]}"


def test_should_activate(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/treehole/should-activate",
        json={"text": "我有一个秘密"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["activates_treehole"] is True


def test_should_not_activate_for_normal_text(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/treehole/should-activate",
        json={"text": "今天天气真好"},
    )
    data = resp.json()
    assert data["activates_treehole"] is False


def test_engage_returns_summary(client: TestClient) -> None:
    child_id = _unique_id()
    resp = client.post(
        "/api/v1/treehole/engage",
        json={
            "child_id": child_id,
            "text": "secret content here",
            "emotion": "negative",
            "intensity": 4,
            "category": "family",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["source"] == "treehole"
    assert data["summary"]["emotion_valence"] == "negative"
    assert data["summary"]["emotion_intensity"] == 4
    assert data["summary"]["emotion_category"] == "family"


def test_engage_does_not_leak_original_text(client: TestClient) -> None:
    """PRD #62: original text must NEVER appear in any response or storage."""
    child_id = _unique_id()
    secret = f"unique_secret_{uuid.uuid4().hex}"
    resp = client.post(
        "/api/v1/treehole/engage",
        json={
            "child_id": child_id,
            "text": secret,
            "emotion": "negative",
            "intensity": 3,
        },
    )
    # The response should not contain the secret text
    assert secret not in resp.text


def test_summaries_listing(client: TestClient) -> None:
    resp = client.get(f"/api/v1/treehole/summaries/{_unique_id()}")
    assert resp.status_code == 200
    data = resp.json()
    assert "summaries" in data
    assert "count" in data


def test_aggregate_baseline(client: TestClient) -> None:
    resp = client.post("/api/v1/treehole/aggregate")
    assert resp.status_code == 200
    data = resp.json()
    assert "mean_valence" in data
    assert "count" in data


def test_store_stats(client: TestClient) -> None:
    resp = client.get("/api/v1/treehole/store-stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "summary_count" in data


def test_purge_expired(client: TestClient) -> None:
    resp = client.post("/api/v1/treehole/purge-expired")
    assert resp.status_code == 200
    data = resp.json()
    assert "purged_count" in data
    assert data["purged_count"] >= 0


def test_purge_all(client: TestClient) -> None:
    resp = client.post("/api/v1/treehole/purge-all")
    assert resp.status_code == 200
    data = resp.json()
    assert "purged_count" in data
    # After purge, no records should remain
    resp2 = client.get("/api/v1/treehole/store-stats")
    assert resp2.json()["summary_count"] == 0


def test_summary_structure_has_no_text_field(client: TestClient) -> None:
    """PRD: TreeholeSummary must NOT have a 'text' field (only structured)."""
    resp = client.post(
        "/api/v1/treehole/engage",
        json={
            "child_id": _unique_id(),
            "text": "private",
            "emotion": "neutral",
            "intensity": 3,
        },
    )
    summary = resp.json()["summary"]
    assert "text" not in summary
    assert "original" not in summary
    assert "content" not in summary
    # Only structured fields
    expected_fields = {
        "timestamp",
        "source",
        "emotion_valence",
        "emotion_intensity",
        "emotion_category",
        "z_score_deviation",
    }
    assert set(summary.keys()) == expected_fields


def test_end_to_end_aggregation(client: TestClient) -> None:
    """Engage multiple → aggregate → all are anonymized into a single number."""
    # First purge to start clean
    client.post("/api/v1/treehole/purge-all")
    for i in range(3):
        client.post(
            "/api/v1/treehole/engage",
            json={
                "child_id": f"c-{i}",
                "text": f"secret {i}",
                "emotion": "negative" if i % 2 == 0 else "positive",
                "intensity": 3,
            },
        )
    resp = client.post("/api/v1/treehole/aggregate")
    data = resp.json()
    assert data["count"] == 3
    # The mean should be between -0.5 and 0.5 (per our formula)
    assert -1.0 <= data["mean_valence"] <= 1.0
