"""Three-level knowledge base — PRD #55.

| Level | Source                          | Use                       | Update |
|-------|---------------------------------|---------------------------|--------|
| L1    | Children's encyclopedia (curated) | Default answer source     | Quarterly |
| L2    | Wikipedia (LLM-simplified)         | L1 fallback                | Monthly  |
| L3    | Live search API                    | Fact-check only            | Real-time |

Design:
- In-memory by default (L1 + L2) — production would use ChromaDB
- All three levels implement the same interface so the agent can use them uniformly
- L3 is async (calls external search API)
- Returns KnowledgeHit objects with source + score for observability
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class KnowledgeSource(StrEnum):
    """Which knowledge level produced a hit."""

    L1_CORE = "l1_core"            # Curated children's encyclopedia
    L2_EXPANDED = "l2_expanded"    # Wikipedia simplified
    L3_LIVE = "l3_live"            # Live search (fact-check only)


@dataclass(frozen=True)
class KnowledgeHit:
    """A single retrieved piece of knowledge."""

    content: str
    source: KnowledgeSource
    score: float
    metadata: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            # Workaround for frozen dataclass: bypass frozen for default mutation
            object.__setattr__(self, "metadata", {})


# Type alias for live-search provider
LiveSearchProvider = Callable[[str], Awaitable[list[str]]]


# --- Default L1 content (curated children's encyclopedia snippets) ---
DEFAULT_L1_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "l1-sky-blue",
        "title": "天空为什么是蓝色的",
        "content": (
            "阳光里有七种颜色（红橙黄绿蓝靛紫），它们是一起到达地面的。"
            "但是空气中的小颗粒喜欢把蓝色的光散开，所以天空看起来是蓝的。"
            "到了傍晚，蓝色光被散得更远了，剩下红橙色的光，所以晚霞是红橙色的。"
        ),
        "tags": ["天空", "颜色", "光", "物理", "自然"],
    },
    {
        "id": "l1-dinosaur",
        "title": "恐龙",
        "content": (
            "恐龙是生活在很久很久以前（几千万年前）的大动物。"
            "它们有很多种类：有的像长颈鹿一样脖子很长（腕龙），"
            "有的会飞（翼龙），有的很凶猛（霸王龙）。"
            "后来地球变冷了，恐龙就消失了。"
        ),
        "tags": ["恐龙", "动物", "远古", "化石"],
    },
    {
        "id": "l1-rain",
        "title": "为什么会下雨",
        "content": (
            "太阳把地面上的水晒热，水变成水蒸气飘到天空。"
            "天上的水蒸气遇到冷空气就变成小水滴。"
            "很多小水滴聚在一起就变成了云。"
            "云里的水滴越来越重，就从天上落下来，这就是雨。"
        ),
        "tags": ["天气", "雨", "水", "自然"],
    },
    {
        "id": "l1-plant",
        "title": "植物是怎么长大的",
        "content": (
            "小小的种子埋进土里，喝到水和阳光，慢慢发芽。"
            "根在地底下吸收水分，茎和叶往上长，去晒太阳。"
            "叶子像小嘴巴一样吸进空气中的二氧化碳，吐出氧气。"
            "慢慢地，种子就长成了一棵植物。"
        ),
        "tags": ["植物", "种子", "自然", "生长"],
    },
    {
        "id": "l1-moon",
        "title": "月亮",
        "content": (
            "月亮是地球的好朋友，它一直绕着地球转。"
            "月亮自己不发光，它反射太阳的光。"
            "我们看到月亮的形状会变化，是因为太阳、地球和月亮的位置在变。"
            "月亮上有很多坑，那是陨石撞出来的。"
        ),
        "tags": ["月亮", "天文", "地球", "太阳"],
    },
    {
        "id": "l1-seed",
        "title": "种子发芽",
        "content": (
            "小小的种子喝到水以后会慢慢变大、破皮，长出一根小芽。"
            "小芽往土外面钻，见到阳光就变成绿色的小苗。"
            "每天给小苗浇水、晒太阳，它就会越长大。"
        ),
        "tags": ["种子", "植物", "发芽", "生长"],
    },
]


class KnowledgeBase:
    """Three-level knowledge base with a simple in-memory L1+L2 implementation.

    L3 (live search) is a callback so callers can wire in any search backend.
    The default L3 is a no-op (returns []) so this works in tests.
    """

    def __init__(
        self,
        l1_entries: list[dict[str, Any]] | None = None,
        l2_entries: list[dict[str, Any]] | None = None,
        live_search: LiveSearchProvider | None = None,
    ) -> None:
        self._l1: list[dict[str, Any]] = l1_entries if l1_entries is not None else list(DEFAULT_L1_ENTRIES)
        self._l2: list[dict[str, Any]] = l2_entries if l2_entries is not None else []
        self._live_search = live_search or (lambda q: asyncio.sleep(0, result=[]))

    def search(self, query: str, top_k: int = 3) -> list[KnowledgeHit]:
        """Search L1 and L2 synchronously.

        Scoring: simple keyword overlap (Jaccard-like). Good enough for the
        in-memory default; production would use embeddings + ChromaDB.

        Args:
            query: The child's question.
            top_k: Maximum number of hits to return.

        Returns:
            A list of KnowledgeHit objects sorted by score descending.
        """
        if not query or not query.strip():
            return []

        hits: list[KnowledgeHit] = []
        for entry in self._l1:
            score = self._score_entry(query, entry)
            if score > 0:
                hits.append(
                    KnowledgeHit(
                        content=f"{entry['title']}：{entry['content']}",
                        source=KnowledgeSource.L1_CORE,
                        score=score,
                        metadata={"id": entry.get("id"), "tags": entry.get("tags", [])},
                    )
                )

        for entry in self._l2:
            score = self._score_entry(query, entry)
            if score > 0:
                hits.append(
                    KnowledgeHit(
                        content=f"{entry['title']}：{entry['content']}",
                        source=KnowledgeSource.L2_EXPANDED,
                        score=score,
                        metadata={"id": entry.get("id"), "tags": entry.get("tags", [])},
                    )
                )

        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]

    async def live_search(self, query: str, top_k: int = 3) -> list[KnowledgeHit]:
        """Search L3 (live web) — for fact-check only (PRD #55).

        Returns empty list when no live_search provider is configured.
        """
        try:
            results = await self._live_search(query)
        except Exception as exc:
            logger.warning("knowledge_base.live_search_failed", error=str(exc))
            return []

        return [
            KnowledgeHit(
                content=r,
                source=KnowledgeSource.L3_LIVE,
                score=0.5,  # Live search hits get a fixed moderate score
                metadata={"query": query},
            )
            for r in results[:top_k]
        ]

    @staticmethod
    def _score_entry(query: str, entry: dict[str, Any]) -> float:
        """Score an entry against the query via token overlap.

        Returns a float in [0.0, 1.0]. Production should use embeddings.
        """
        # Tokenize: split Chinese chars and English words
        query_tokens = set(_tokenize(query))
        title_tokens = set(_tokenize(entry.get("title", "")))
        content_tokens = set(_tokenize(entry.get("content", "")))
        tag_tokens = set()
        for tag in entry.get("tags", []):
            tag_tokens.update(_tokenize(tag))

        if not query_tokens:
            return 0.0

        all_doc_tokens = title_tokens | content_tokens | tag_tokens
        if not all_doc_tokens:
            return 0.0

        overlap = query_tokens & all_doc_tokens
        # Jaccard + title-match bonus
        jaccard = len(overlap) / len(query_tokens | all_doc_tokens)
        title_match_bonus = 0.3 if title_tokens & query_tokens else 0.0
        tag_match_bonus = 0.2 if tag_tokens & query_tokens else 0.0

        return min(1.0, jaccard * 2 + title_match_bonus + tag_match_bonus)


def _tokenize(text: str) -> list[str]:
    """Tokenize text into words/chars suitable for keyword matching.

    - For Chinese: split into individual characters (since Chinese has no spaces)
    - For English: split on whitespace, lowercase
    - Returns combined list
    """
    if not text:
        return []

    # Extract CJK characters individually
    cjk_chars = re.findall(r"[一-鿿]", text)
    # Extract English words
    english_words = re.findall(r"[a-zA-Z]+", text.lower())
    return cjk_chars + english_words
