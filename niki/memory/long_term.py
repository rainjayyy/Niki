"""Long-term memory — PRD #45.

ChromaDB-backed vector store of:
- Conversation embeddings (with emotion values, topic tags, timestamps)
- Knowledge queries the child made
- Vocabulary learned

Each record has: id, content, embedding (auto-generated), metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import structlog

from niki.infrastructure.chroma import COLLECTION_CONVERSATIONS, ChromaClient

logger = structlog.get_logger(__name__)


class MemorySource(StrEnum):
    """Where the memory record came from."""

    DAYTIME_HOOK = "daytime_hook"
    NIGHTLY_PIPELINE = "nightly_pipeline"
    KNOWLEDGE_QUERY = "knowledge_query"
    TUTORING_EVENT = "tutoring_event"
    TREEHOLE_ENGAGEMENT = "treehole_engagement"


@dataclass
class MemoryRecord:
    """A single long-term memory record."""

    id: str
    content: str
    source: MemorySource
    child_id: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    emotion: str | None = None
    intensity: int | None = None
    topic: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "content": self.content,
            "source": self.source.value,
            "child_id": self.child_id,
            "timestamp": self.timestamp.isoformat(),
            "emotion": self.emotion,
            "intensity": self.intensity,
            "topic": self.topic,
            "metadata": self.metadata,
        }
        return d


class LongTermMemory:
    """Vector-store backed by ChromaDB (production) or in-memory (tests)."""

    def __init__(self, collection_name: str = COLLECTION_CONVERSATIONS) -> None:
        self.collection_name = collection_name
        self._in_memory: list[MemoryRecord] = []
        # Try to connect to Chroma
        self._chroma = None
        try:
            self._chroma = ChromaClient.get_client()
        except Exception as exc:
            logger.warning(
                "long_term.chroma_unavailable",
                error=str(exc),
                fallback="in_memory",
            )

    async def add(self, record: MemoryRecord) -> str:
        """Add a record. Returns the record id."""
        self._in_memory.append(record)

        # Try Chroma
        if self._chroma is not None:
            try:
                coll = self._chroma.get_collection(self.collection_name)
                # Run in thread (chromadb is sync)
                await asyncio.to_thread(
                    coll.add,
                    documents=[record.content],
                    metadatas=[
                        {
                            "child_id": record.child_id,
                            "source": record.source.value,
                            "emotion": record.emotion or "",
                            "intensity": record.intensity or 0,
                            "topic": record.topic or "",
                            "timestamp": record.timestamp.isoformat(),
                            **record.metadata,
                        }
                    ],
                    ids=[record.id],
                )
            except Exception as exc:
                logger.warning(
                    "long_term.chroma_add_failed",
                    error=str(exc),
                    fallback="memory only",
                )
        return record.id

    async def search(
        self,
        query: str,
        child_id: str | None = None,
        n_results: int = 5,
    ) -> list[MemoryRecord]:
        """Search for similar memories."""
        # Try Chroma first
        if self._chroma is not None:
            try:
                coll = self._chroma.get_collection(self.collection_name)
                where = {"child_id": child_id} if child_id else None
                results = await asyncio.to_thread(
                    coll.query,
                    query_texts=[query],
                    n_results=n_results,
                    where=where,
                )
                return self._parse_chroma_results(results)
            except Exception as exc:
                logger.warning(
                    "long_term.chroma_query_failed",
                    error=str(exc),
                    fallback="memory",
                )

        # In-memory fallback: simple keyword match
        results = []
        for r in self._in_memory:
            if child_id and r.child_id != child_id:
                continue
            if any(kw in r.content for kw in query.split()):
                results.append(r)
        return results[:n_results]

    def _parse_chroma_results(self, results: dict) -> list[MemoryRecord]:
        """Convert Chroma query results to MemoryRecord list."""
        records = []
        if not results or not results.get("ids"):
            return records
        ids = results["ids"][0] if results.get("ids") else []
        docs = results.get("documents", [[]])[0] if results.get("documents") else []
        metas = results.get("metadatas", [[]])[0] if results.get("metadatas") else []
        for record_id, doc, meta in zip(ids, docs, metas, strict=False):
            records.append(
                MemoryRecord(
                    id=record_id,
                    content=doc,
                    source=MemorySource(meta.get("source", "daytime_hook")),
                    child_id=meta.get("child_id", ""),
                    timestamp=datetime.fromisoformat(meta["timestamp"])
                    if meta.get("timestamp")
                    else datetime.utcnow(),
                    emotion=meta.get("emotion") or None,
                    intensity=meta.get("intensity") or None,
                    topic=meta.get("topic") or None,
                )
            )
        return records

    async def count(self, child_id: str | None = None) -> int:
        """Count records (optionally for a child)."""
        if child_id is None:
            return len(self._in_memory)
        return sum(1 for r in self._in_memory if r.child_id == child_id)

    async def clear_for_child(self, child_id: str) -> int:
        """Erase all records for a child (used for account deletion)."""
        before = len(self._in_memory)
        self._in_memory = [r for r in self._in_memory if r.child_id != child_id]
        return before - len(self._in_memory)

    @staticmethod
    def make_id(child_id: str, content: str, source: str) -> str:
        """Generate a deterministic memory id."""
        raw = f"{child_id}:{source}:{content[:100]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
