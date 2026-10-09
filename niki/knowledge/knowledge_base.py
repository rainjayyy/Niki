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

    L1_CORE = "l1_core"  # Curated children's encyclopedia
    L2_EXPANDED = "l2_expanded"  # Wikipedia simplified
    L3_LIVE = "l3_live"  # Live search (fact-check only)


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
        self._l1: list[dict[str, Any]] = (
            l1_entries if l1_entries is not None else list(DEFAULT_L1_ENTRIES)
        )
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
    {
        "id": "l1-sea-salt",
        "title": "海水为什么是咸的",
        "content": (
            "陆地上的雨水流进小河，小河流进大海的时候，会把石头和土壤里"
            "一点点咸的盐分一起带进海里。海水被太阳晒干后，盐会留下来。"
            "不过海水不会真的晒干，所以盐就一直留在海里，海水就变得咸咸的啦~"
        ),
        "tags": ["海水", "盐", "海洋", "自然"],
    },
    {
        "id": "l1-moon-shape",
        "title": "月亮为什么有时圆有时弯",
        "content": (
            "月亮自己不发光，它像一面大镜子反射太阳的光。"
            "月亮绕着地球转，当它转到不同位置时，被太阳照亮的部分不一样大。"
            "所以我们有时看到圆圆的满月，有时看到弯弯的月牙~"
        ),
        "tags": ["月亮", "月相", "天文", "太阳"],
    },
    {
        "id": "l1-rainbow",
        "title": "彩虹是怎么形成的",
        "content": (
            "下雨之后空气里有很多小水滴。太阳光照到这些小水滴上，"
            "会像三棱镜一样分成七种颜色：红橙黄绿蓝靛紫。"
            "这些颜色弯弯地连在一起，就变成了天上的彩虹啦~"
        ),
        "tags": ["彩虹", "颜色", "光", "下雨", "太阳"],
    },
    {
        "id": "l1-whale",
        "title": "鲸鱼不是鱼",
        "content": (
            "鲸鱼虽然名字里有「鱼」，生活在海里，但它其实是哺乳动物哦！"
            "鲸鱼用肺呼吸，需要到水面上换气；宝宝是喝妈妈的奶长大的。"
            "蓝鲸是世界上最大的动物，比任何恐龙都大~"
        ),
        "tags": ["鲸鱼", "海洋动物", "哺乳动物", "动物"],
    },
    {
        "id": "l1-electricity",
        "title": "电是怎么来的",
        "content": (
            "电是一种能量。发电厂的巨大磁铁转得很快，"
            "就能让铜线里的小电子跑起来，这些跑动的小电子就是电~"
            "电通过电线送到家里，电视、灯、风扇就能工作了。"
        ),
        "tags": ["电", "能量", "发电", "物理"],
    },
    {
        "id": "l1-water-cycle",
        "title": "水循环",
        "content": (
            "太阳把河海里的水晒热，水蒸气飘到天上变成云。"
            "云里的小水滴越聚越多，变重了就落下来变成雨或雪。"
            "雨水流回河里，河水又流进大海，水就这样一直循环~"
        ),
        "tags": ["水", "循环", "雨", "蒸发", "自然"],
    },
    {
        "id": "l1-addition",
        "title": "加法是什么",
        "content": (
            "加法就是把两组东西合在一起，看看一共有多少。"
            "比如你有 2 个苹果，妈妈再给你 3 个，加起来是 5 个啦~"
            "符号是「+」，比如 2+3=5。但 Niki 不能直接告诉你答案，要先让你自己数~"
        ),
        "tags": ["数学", "加法", "计算"],
    },
]
