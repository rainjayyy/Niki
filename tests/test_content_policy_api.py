"""End-to-end API tests for the content policy endpoints."""

import uuid

from fastapi.testclient import TestClient


def _family_id() -> str:
    return f"family-{uuid.uuid4().hex[:16]}"


# ---- Blacklist endpoints ----


def test_blacklist_empty(client: TestClient) -> None:
    fid = _family_id()
    resp = client.get(f"/api/v1/policy/blacklist/{fid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["size"] == 0


def test_blacklist_add(client: TestClient) -> None:
    fid = _family_id()
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/add",
        json={"topic": "打游戏", "action": "downgrade", "added_by": "p1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["blacklist_size"] == 1
    assert data["entry"]["topic"] == "打游戏"


def test_blacklist_add_invalid_action_defaults(client: TestClient) -> None:
    fid = _family_id()
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/add",
        json={"topic": "test", "action": "invalid_action"},
    )
    assert resp.json()["entry"]["action"] == "downgrade"


def test_blacklist_remove(client: TestClient) -> None:
    fid = _family_id()
    client.post(f"/api/v1/policy/blacklist/{fid}/add", json={"topic": "X"})
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/remove",
        json={"topic": "X"},
    )
    assert resp.json()["removed"] is True


def test_blacklist_remove_nonexistent(client: TestClient) -> None:
    fid = _family_id()
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/remove",
        json={"topic": "never_added"},
    )
    assert resp.json()["removed"] is False


def test_blacklist_evaluate_match(client: TestClient) -> None:
    fid = _family_id()
    client.post(f"/api/v1/policy/blacklist/{fid}/add", json={"topic": "游戏"})
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/evaluate",
        json={"text": "我想玩游戏"},
    )
    data = resp.json()
    assert data["match_count"] == 1


def test_blacklist_evaluate_no_match(client: TestClient) -> None:
    fid = _family_id()
    resp = client.post(
        f"/api/v1/policy/blacklist/{fid}/evaluate",
        json={"text": "我今天很开心"},
    )
    assert resp.json()["match_count"] == 0


# ---- Age grading endpoints ----


def test_get_age_band(client: TestClient) -> None:
    resp = client.get("/api/v1/policy/age/child-1?child_age=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["child_age"] == 7
    assert data["age_band"] == 6  # ContentAge.YOUNG


def test_grade_concept_appropriate(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/policy/age/grade",
        json={"child_age": 4, "concept": "恐龙"},
    )
    data = resp.json()
    assert data["appropriate"] is True


def test_grade_concept_too_advanced(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/policy/age/grade",
        json={"child_age": 4, "concept": "量子力学"},
    )
    data = resp.json()
    assert data["appropriate"] is False


def test_grade_text_multiple_concepts(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/policy/age/grade-text",
        json={"child_age": 7, "text": "我喜欢恐龙和颜色"},
    )
    data = resp.json()
    assert data["count"] >= 2


def test_grade_text_no_concepts(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/policy/age/grade-text",
        json={"child_age": 7, "text": "unrelated text without any concepts"},
    )
    assert resp.json()["count"] == 0


# ---- Downgrade evaluate endpoint ----


def test_downgrade_evaluate_pass(client: TestClient) -> None:
    fid = _family_id()
    cid = f"child-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/policy/downgrade/evaluate",
        json={
            "family_id": fid,
            "child_id": cid,
            "child_age": 4,
            "text": "我喜欢恐龙",
        },
    )
    data = resp.json()
    assert data["action"] == "pass"


def test_downgrade_evaluate_blacklist(client: TestClient) -> None:
    fid = _family_id()
    cid = f"child-{uuid.uuid4().hex[:8]}"
    client.post(f"/api/v1/policy/blacklist/{fid}/add", json={"topic": "游戏"})
    resp = client.post(
        "/api/v1/policy/downgrade/evaluate",
        json={
            "family_id": fid,
            "child_id": cid,
            "child_age": 7,
            "text": "我想玩游戏",
        },
    )
    data = resp.json()
    assert data["action"] == "deflect"
    assert data["blocked_concept"] == "游戏"


def test_downgrade_evaluate_age(client: TestClient) -> None:
    fid = _family_id()
    cid = f"child-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/policy/downgrade/evaluate",
        json={
            "family_id": fid,
            "child_id": cid,
            "child_age": 4,
            "text": "量子力学 概念",
        },
    )
    data = resp.json()
    # Either SIMPLIFY (if replacement exists) or SOFTEN (if not)
    assert data["action"] in ("simplify", "soften")
