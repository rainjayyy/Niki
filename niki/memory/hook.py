"""Daytime memory hook — PRD #48.

Real-time extraction of high-value information from conversations:
- New entities (people, places, topics)
- New preferences
- New emotion events

PRD: delay < 500ms (so the next message can use the new memory).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

from niki.memory.graph import CognitiveGraph, EntityAligner, EntityNode, RelationType
from niki.memory.long_term import LongTermMemory, MemoryRecord, MemorySource

logger = structlog.get_logger(__name__)


class ExtractionKind(StrEnum):
    """Type of extracted information."""

    NEW_ENTITY = "new_entity"  # "我的小狗叫旺财"
    NEW_PREFERENCE = "new_preference"  # "我喜欢恐龙"
    NEW_EMOTION = "new_emotion"  # "我今天很难过"
    NEW_VOCAB = "new_vocab"  # "暴龙是..."
    NEW_FACT = "new_fact"  # "我的生日是X月X日"


@dataclass
class ExtractionCandidate:
    """A single piece of information extracted from a message."""

    kind: ExtractionKind
    text: str
    child_id: str
    confidence: float
    context: str = ""  # surrounding text
    metadata: dict[str, Any] = field(default_factory=dict)
    extracted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DaytimeMemoryHook:
    """Real-time extraction of high-value info from child messages.

    Pattern-based (rule-based) for fast, deterministic extraction.
    Production would augment with an LLM for richer extraction.
    """

    # Patterns for extraction
    ENTITY_PATTERNS = [
        re.compile(r"我(?:的|有一个?)?(\w{2,8})叫(\w{2,8})"),  # "我的小狗叫旺财"
        re.compile(r"我(?:喜欢|讨厌|害怕|想念)(\w{2,8})"),  # "我喜欢恐龙"
        re.compile(r"(\w{2,8})是(?:我的|我们家的)", re.IGNORECASE),  # "旺财是我的狗"
    ]

    EMOTION_PATTERNS = [
        re.compile(r"我(?:今天)?(?:很|有点|非常)?(开心|难过|伤心|生气|害怕|紧张)"),
        re.compile(r"我(?:觉得|感觉)(?:\w+?)(开心|难过|伤心|生气|害怕)"),
    ]

    def __init__(
        self,
        long_term: LongTermMemory,
        graph: CognitiveGraph,
    ) -> None:
        self._long_term = long_term
        self._graph = graph
        self._aligner = EntityAligner(graph)

    async def process(
        self,
        child_id: str,
        text: str,
        context: dict[str, Any] | None = None,
    ) -> list[ExtractionCandidate]:
        """Extract candidates from a child message.

        Args:
            child_id: The child's id.
            text: The child's text.
            context: Optional context (intent, emotion, etc.).

        Returns:
            List of extraction candidates (some may have been persisted).
        """
        candidates: list[ExtractionCandidate] = []
        ctx = context or {}

        # 1. Extract entities
        for match in self._extract_entities(text):
            candidates.append(match)
            entity = self._aligner.align(
                EntityNode(
                    name=match.text,
                    entity_type=ctx.get("entity_type", "concept"),
                )
            )
            # Add a "remembers" relation to the child
            self._graph.add_relation(
                source=child_id,
                target=entity.name,
                relation=RelationType.REMEMBERS,
                weight_delta=0.2,
            )
            # Persist to long-term memory
            record_id = LongTermMemory.make_id(child_id, match.text, "daytime_hook")
            await self._long_term.add(
                MemoryRecord(
                    id=record_id,
                    content=match.text,
                    source=MemorySource.DAYTIME_HOOK,
                    child_id=child_id,
                    emotion=ctx.get("emotion"),
                    intensity=ctx.get("intensity"),
                    topic=match.text,
                )
            )

        # 2. Extract emotions
        for match in self._extract_emotions(text):
            candidates.append(match)

        # 3. Extract preferences (entities already cover "I like X" — handled above)
        return candidates

    def _extract_entities(self, text: str) -> list[ExtractionCandidate]:
        """Extract entity mentions from text."""
        out = []
        for pattern in self.ENTITY_PATTERNS:
            for m in pattern.finditer(text):
                # Pick the most specific token
                token = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
                if len(token) >= 2:
                    out.append(
                        ExtractionCandidate(
                            kind=ExtractionKind.NEW_ENTITY,
                            text=token,
                            child_id="",
                            confidence=0.6,
                            context=text[max(0, m.start() - 10) : m.end() + 10],
                        )
                    )
        return out

    def _extract_emotions(self, text: str) -> list[ExtractionCandidate]:
        out = []
        for pattern in self.EMOTION_PATTERNS:
            for m in pattern.finditer(text):
                emotion = m.group(1)
                if emotion:
                    out.append(
                        ExtractionCandidate(
                            kind=ExtractionKind.NEW_EMOTION,
                            text=emotion,
                            child_id="",
                            confidence=0.7,
                            context=text[max(0, m.start() - 10) : m.end() + 10],
                        )
                    )
        return out
