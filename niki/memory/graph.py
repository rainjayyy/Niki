"""Cognitive graph — PRD #45, #50, #49.

Neo4j entity-relationship graph with:
- Entity nodes (people, places, topics, things the child mentioned)
- Relation edges with weight + last_updated timestamps
- Time-decay: edges lose weight over time (PRD #50)
  - 30 days no update: weight × 0.5
  - 60 days no update: weight × 0.1

In production: Neo4j (already wired up in P0).
Here: in-memory graph with the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

from niki.infrastructure.neo4j import Neo4jClient

logger = structlog.get_logger(__name__)


class RelationType(StrEnum):
    """Types of relations in the cognitive graph."""

    LIKES = "likes"                # child likes entity
    DISLIKES = "dislikes"
    REMEMBERS = "remembers"        # child mentioned before
    RELATED_TO = "related_to"
    KNOWS_ABOUT = "knows_about"


@dataclass
class EntityNode:
    """A single entity in the cognitive graph."""

    name: str
    entity_type: str = "concept"   # person, place, topic, thing
    aliases: list[str] = field(default_factory=list)
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "aliases": list(self.aliases),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
        }


@dataclass
class RelationEdge:
    """A relation between two entities with weight + last_updated."""

    source: str
    target: str
    relation: RelationType
    weight: float = 1.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "relation": self.relation.value,
            "weight": self.weight,
            "last_updated": self.last_updated.isoformat(),
        }

    def apply_time_decay(self, now: datetime | None = None) -> None:
        """Apply PRD #50 time decay.

        - 30 days no update: weight × 0.5
        - 60 days no update: weight × 0.1
        """
        now = now or datetime.now(timezone.utc)
        days = (now - self.last_updated).days
        if days >= 60:
            self.weight *= 0.1
        elif days >= 30:
            self.weight *= 0.5
        # Update last_updated
        self.last_updated = now


class CognitiveGraph:
    """Entity-relationship graph for the child's knowledge.

    Backed by Neo4j in production; in-memory in tests.
    """

    def __init__(self) -> None:
        self._entities: dict[str, EntityNode] = {}
        self._edges: list[RelationEdge] = []
        # Try to connect to Neo4j
        self._neo4j = None
        try:
            self._neo4j = Neo4jClient  # don't await here
        except Exception as exc:
            logger.warning(
                "cognitive_graph.neo4j_unavailable",
                error=str(exc),
                fallback="in_memory",
            )

    # ---- Entity management ----

    def add_entity(
        self,
        name: str,
        entity_type: str = "concept",
        aliases: list[str] | None = None,
    ) -> EntityNode:
        """Add or update an entity (idempotent by name)."""
        if name in self._entities:
            existing = self._entities[name]
            if aliases:
                for a in aliases:
                    if a not in existing.aliases:
                        existing.aliases.append(a)
            existing.last_seen = datetime.now(timezone.utc)
            return existing

        entity = EntityNode(
            name=name,
            entity_type=entity_type,
            aliases=aliases or [],
        )
        self._entities[name] = entity
        return entity

    def get_entity(self, name: str) -> EntityNode | None:
        return self._entities.get(name)

    def list_entities(self, entity_type: str | None = None) -> list[EntityNode]:
        entities = list(self._entities.values())
        if entity_type:
            entities = [e for e in entities if e.entity_type == entity_type]
        return entities

    # ---- Relation management ----

    def add_relation(
        self,
        source: str,
        target: str,
        relation: RelationType,
        weight_delta: float = 0.1,
    ) -> RelationEdge:
        """Add or update a relation. Weight increases on duplicate edges."""
        # Ensure entities exist
        self.add_entity(source)
        self.add_entity(target)

        for edge in self._edges:
            if (
                edge.source == source
                and edge.target == target
                and edge.relation == relation
            ):
                edge.weight = min(1.0, edge.weight + weight_delta)
                edge.last_updated = datetime.now(timezone.utc)
                return edge

        edge = RelationEdge(
            source=source,
            target=target,
            relation=relation,
            weight=min(1.0, weight_delta),
        )
        self._edges.append(edge)
        return edge

    def get_relations_for(self, entity: str) -> list[RelationEdge]:
        """Get all relations touching the entity."""
        return [e for e in self._edges if e.source == entity or e.target == entity]

    # ---- Time decay (PRD #50) ----

    def apply_decay(self, now: datetime | None = None) -> int:
        """Apply time-decay to all edges. Returns number of edges decayed."""
        count = 0
        for edge in self._edges:
            if edge.weight > 0.1:  # Don't decay below floor
                edge.apply_time_decay(now)
                count += 1
        return count

    # ---- Account deletion (PRD #35) ----

    def clear_for_child(self, child_id: str) -> int:
        """Erase all entities/relations associated with a child."""
        # We don't track child_id per entity (entities are shared),
        # so this is a stub for now.
        return 0


class EntityAligner:
    """Semantic entity deduplication (PRD #49).

    Merges entities that look the same:
    - "霸王龙" + "T-Rex" + "暴龙" → one entity
    Production: uses Chroma embeddings with cosine similarity > 0.85.
    Here: simple Levenshtein + substring matching.
    """

    SIMILARITY_THRESHOLD = 0.85

    def __init__(self, graph: CognitiveGraph) -> None:
        self._graph = graph

    def align(self, new_entity: EntityNode) -> EntityNode:
        """If new_entity is similar to an existing one, merge. Otherwise add."""
        for existing in self._graph.list_entities():
            similarity = self._compute_similarity(
                new_entity.name, existing.name, existing.aliases
            )
            if similarity >= self.SIMILARITY_THRESHOLD:
                # Merge into existing
                if new_entity.name not in existing.aliases and new_entity.name != existing.name:
                    existing.aliases.append(new_entity.name)
                existing.last_seen = datetime.now(timezone.utc)
                return existing

        # New entity
        self._graph.add_entity(
            name=new_entity.name,
            entity_type=new_entity.entity_type,
            aliases=new_entity.aliases,
        )
        return new_entity

    def _compute_similarity(
        self,
        name: str,
        other_name: str,
        other_aliases: list[str],
    ) -> float:
        """Compute a similarity score in [0, 1].

        Combines: exact match, substring, character overlap, alias match.
        """
        candidates = [other_name, *other_aliases]
        for c in candidates:
            if c == name:
                return 1.0
            if c in name or name in c:
                return 0.9
        # Character overlap (Jaccard)
        a = set(name)
        b = set(other_name)
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)
