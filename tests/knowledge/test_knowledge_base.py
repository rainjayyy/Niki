"""Tests for the 3-level knowledge base."""

import pytest

from niki.knowledge.knowledge_base import (
    DEFAULT_L1_ENTRIES,
    KnowledgeBase,
    KnowledgeHit,
    KnowledgeSource,
)


@pytest.fixture
def kb() -> KnowledgeBase:
    return KnowledgeBase()


class TestL1KnowledgeBase:
    """L1 — curated children's encyclopedia."""

    def test_default_has_entries(self, kb: KnowledgeBase) -> None:
        assert len(DEFAULT_L1_ENTRIES) >= 5

    def test_search_finds_sky_blue(self, kb: KnowledgeBase) -> None:
        hits = kb.search("为什么天是蓝的")
        assert len(hits) >= 1
        # Top hit should be about sky
        assert "蓝" in hits[0].content or "天" in hits[0].content
        assert hits[0].source == KnowledgeSource.L1_CORE

    def test_search_finds_dinosaur(self, kb: KnowledgeBase) -> None:
        hits = kb.search("恐龙")
        assert len(hits) >= 1
        assert "恐龙" in hits[0].content

    def test_search_finds_rain(self, kb: KnowledgeBase) -> None:
        hits = kb.search("为什么会下雨")
        assert len(hits) >= 1
        assert "雨" in hits[0].content

    def test_search_unknown_returns_empty(self, kb: KnowledgeBase) -> None:
        """A truly unknown query should have no high-relevance hits.

        Some hits may exist with very low scores (because of shared CJK
        characters), so we check the *relevance* rather than raw count.
        """
        hits = kb.search("火星人住在哪个星系")
        # If any hits exist, they should have very low scores
        for h in hits:
            assert h.score < 0.15, f"Unexpectedly high score: {h}"

    def test_search_empty_query_returns_empty(self, kb: KnowledgeBase) -> None:
        assert kb.search("") == []
        assert kb.search("   ") == []

    def test_search_top_k(self, kb: KnowledgeBase) -> None:
        hits = kb.search("自然", top_k=2)
        assert len(hits) <= 2

    def test_search_returns_sorted_by_score(self, kb: KnowledgeBase) -> None:
        hits = kb.search("天空蓝色")
        if len(hits) > 1:
            for i in range(len(hits) - 1):
                assert hits[i].score >= hits[i + 1].score

    def test_search_includes_metadata(self, kb: KnowledgeBase) -> None:
        hits = kb.search("天空")
        assert hits
        assert hits[0].metadata is not None
        assert "tags" in hits[0].metadata or "id" in hits[0].metadata


class TestL2KnowledgeBase:
    """L2 — expanded (Wikipedia simplified) layer."""

    def test_l2_empty_by_default(self) -> None:
        kb = KnowledgeBase()
        assert kb._l2 == []

    def test_l2_entries_searchable(self) -> None:
        l2_entry = {
            "id": "l2-test",
            "title": "黑洞",
            "content": "黑洞是一种引力极强的天体，连光都跑不出来。",
            "tags": ["天文", "黑洞", "宇宙"],
        }
        kb = KnowledgeBase(l2_entries=[l2_entry])
        hits = kb.search("黑洞")
        assert len(hits) >= 1
        assert hits[0].source == KnowledgeSource.L2_EXPANDED

    def test_l1_outranks_l2_when_both_match(self) -> None:
        l1 = {
            "id": "l1-dup",
            "title": "太阳",
            "content": "太阳是恒星。",
            "tags": ["太阳"],
        }
        l2 = {
            "id": "l2-dup",
            "title": "太阳",
            "content": "太阳是恒星。",
            "tags": ["太阳"],
        }
        kb = KnowledgeBase(l1_entries=[l1], l2_entries=[l2])
        hits = kb.search("太阳")
        assert hits
        # L1 entries have title-match bonus
        assert hits[0].source == KnowledgeSource.L1_CORE


class TestL3LiveSearch:
    """L3 — live search (for fact-check only)."""

    async def test_live_search_no_provider_returns_empty(self) -> None:
        kb = KnowledgeBase()  # no live_search provider
        hits = await kb.live_search("any query")
        assert hits == []

    async def test_live_search_uses_provider(self) -> None:
        async def provider(query: str) -> list[str]:
            return [f"Live result for {query}", "Another fact"]

        kb = KnowledgeBase(live_search=provider)
        hits = await kb.live_search("test query", top_k=2)
        assert len(hits) == 2
        assert all(h.source == KnowledgeSource.L3_LIVE for h in hits)
        assert "Live result" in hits[0].content

    async def test_live_search_respects_top_k(self) -> None:
        async def provider(query: str) -> list[str]:
            return [f"r{i}" for i in range(10)]

        kb = KnowledgeBase(live_search=provider)
        hits = await kb.live_search("q", top_k=3)
        assert len(hits) == 3

    async def test_live_search_handles_provider_error(self) -> None:
        async def broken_provider(query: str) -> list[str]:
            raise RuntimeError("API down")

        kb = KnowledgeBase(live_search=broken_provider)
        hits = await kb.live_search("q")
        assert hits == []  # graceful failure


class TestKnowledgeHit:
    """KnowledgeHit dataclass."""

    def test_metadata_default_empty(self) -> None:
        hit = KnowledgeHit(
            content="test",
            source=KnowledgeSource.L1_CORE,
            score=0.5,
        )
        assert hit.metadata == {}

    def test_metadata_preserved(self) -> None:
        hit = KnowledgeHit(
            content="test",
            source=KnowledgeSource.L1_CORE,
            score=0.5,
            metadata={"id": "abc"},
        )
        assert hit.metadata["id"] == "abc"
