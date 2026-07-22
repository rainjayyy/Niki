"""End-to-end API tests for the encyclopedia endpoints."""

from fastapi.testclient import TestClient


def test_ask_known_topic(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "为什么天是蓝的", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "text" in data
    assert "蓝" in data["text"] or "天空" in data["text"]


def test_ask_unknown_topic_admits_uncertainty(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "火星人住在开普勒-186f", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "不确定" in data["text"] or "问问" in data["text"]


def test_ask_with_age_3_to_5(client: TestClient) -> None:
    """A 3-year-old should still get an answer (possibly shorter)."""
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "恐龙", "child_age": 4},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "恐龙" in data["text"]


def test_ask_with_age_9_to_12(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "月亮", "child_age": 11},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "月亮" in data["text"]


def test_ask_metadata_includes_sources(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "天空蓝色", "child_age": 7},
    )
    data = resp.json()
    assert "metadata" in data
    assert "sources" in data["metadata"]
    assert "l1_core" in data["metadata"]["sources"]


def test_ask_empty_question(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/ask",
        json={"question": "", "child_age": 7},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "再说一次" in data["text"] or "没听清" in data["text"]


def test_knowledge_list(client: TestClient) -> None:
    resp = client.get("/api/v1/encyclopedia/knowledge")
    assert resp.status_code == 200
    data = resp.json()
    assert data["level"] == "L1"
    assert data["count"] >= 5
    # Each entry has id, title, tags
    for entry in data["entries"]:
        assert "id" in entry
        assert "title" in entry


def test_search_kb(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/search",
        json={"query": "天空", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["query"] == "天空"
    assert data["hit_count"] >= 1
    # Each hit has source, score, content
    for hit in data["hits"]:
        assert "source" in hit
        assert "score" in hit
        assert "content" in hit


def test_search_kb_no_results(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/search",
        json={"query": "火星人住在开普勒-186f", "top_k": 3},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Even unknown topics may have low-score hits; just ensure top_k respected
    assert data["hit_count"] <= 3
    # Top hit should have very low score (irrelevant)
    if data["hits"]:
        assert data["hits"][0]["score"] < 0.5


def test_search_kb_respects_top_k(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/encyclopedia/search",
        json={"query": "自然", "top_k": 1},
    )
    data = resp.json()
    assert data["hit_count"] <= 1
