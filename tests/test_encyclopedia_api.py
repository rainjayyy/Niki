"""End-to-end API tests for the encyclopedia endpoints.

These tests cover the API contract (request shape, response shape,
error handling) — NOT the LLM's content quality. The actual LLM
output is non-deterministic across model versions, so the tests
force stub mode to get deterministic, faithful-paraphrase
responses.
"""

import pytest
from fastapi.testclient import TestClient

from niki.api import encyclopedia as api_encyclopedia
from niki.api._fallback_llm import FallbackEncyclopediaLLM


@pytest.fixture(autouse=True)
def _stub_encyclopedia_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the encyclopedia endpoint to use the deterministic
    ``FallbackEncyclopediaLLM`` rather than the real MiniMax / DeepSeek
    LLM. The tests assert on specific response keywords that the
    stub faithfully produces but the real LLM is free to
    paraphrase.

    We patch the agent cache so all per-child_age agents are
    rebuilt with the stub, and the ``_fact_checker`` so its
    LLM-judge path doesn't try to ping the real LLM either.

    We also clear the ``get_settings()`` LRU cache so the
    ``agent_tool_use_enabled`` flag (set by the orchestrator
    streaming tests via monkeypatch + cache_clear()) doesn't
    leak into the encyclopedia tests — with the tool loop
    enabled, the agent would try to drive the stub through
    tool calls and fail.
    """
    from niki.config.settings import get_settings

    get_settings.cache_clear()
    stub = FallbackEncyclopediaLLM()
    monkeypatch.setattr(api_encyclopedia, "_llm", stub)
    # Rebuild the default + per-child-age agent cache with the
    # stub LLM. The fact_checker keeps ``llm_judge=None`` because
    # the stub doesn't speak the JSON fact-checker protocol.
    from niki.agents.encyclopedia import EncyclopediaAgent

    api_encyclopedia._agent_cache.clear()  # noqa: SLF001
    from niki.knowledge.fact_checker import FactChecker

    monkeypatch.setattr(
        api_encyclopedia,
        "_fact_checker",
        FactChecker(api_encyclopedia._kb, llm_judge=None),  # noqa: SLF001
    )
    # Re-seed the cache for the default child_age=7 agent
    api_encyclopedia._agent_cache[7] = EncyclopediaAgent(  # noqa: SLF001
        llm=stub,
        knowledge_base=api_encyclopedia._kb,  # noqa: SLF001
        fact_checker=api_encyclopedia._fact_checker,  # noqa: SLF001
        persona="gentle",
        child_age=7,
        tool_checker=api_encyclopedia._tool_checker,  # noqa: SLF001
    )


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
