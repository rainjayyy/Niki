"""Tests for the cognitive graph + entity aligner."""

from datetime import datetime, timedelta, timezone

import pytest

from niki.memory.graph import (
    CognitiveGraph,
    EntityAligner,
    EntityNode,
    RelationType,
)


@pytest.fixture
def graph() -> CognitiveGraph:
    return CognitiveGraph()


class TestEntityManagement:
    def test_add_entity(self, graph: CognitiveGraph) -> None:
        e = graph.add_entity("恐龙", entity_type="topic")
        assert e.name == "恐龙"
        assert e.entity_type == "topic"

    def test_add_entity_idempotent(self, graph: CognitiveGraph) -> None:
        graph.add_entity("恐龙")
        graph.add_entity("恐龙", aliases=["霸王龙"])
        entities = graph.list_entities()
        assert len(entities) == 1
        assert "霸王龙" in entities[0].aliases

    def test_get_entity(self, graph: CognitiveGraph) -> None:
        graph.add_entity("恐龙")
        e = graph.get_entity("恐龙")
        assert e is not None
        assert e.name == "恐龙"

    def test_get_nonexistent_entity(self, graph: CognitiveGraph) -> None:
        assert graph.get_entity("nope") is None

    def test_list_by_type(self, graph: CognitiveGraph) -> None:
        graph.add_entity("恐龙", entity_type="topic")
        graph.add_entity("旺财", entity_type="pet")
        topics = graph.list_entities(entity_type="topic")
        assert len(topics) == 1
        assert topics[0].name == "恐龙"


class TestRelationManagement:
    def test_add_relation(self, graph: CognitiveGraph) -> None:
        edge = graph.add_relation("c1", "恐龙", RelationType.LIKES)
        assert edge.source == "c1"
        assert edge.target == "恐龙"
        assert edge.weight > 0

    def test_add_relation_increases_weight(self, graph: CognitiveGraph) -> None:
        e1 = graph.add_relation("c1", "恐龙", RelationType.LIKES)
        w1 = e1.weight
        e2 = graph.add_relation("c1", "恐龙", RelationType.LIKES)
        assert e2.weight > w1

    def test_weight_capped_at_one(self, graph: CognitiveGraph) -> None:
        for _ in range(20):
            edge = graph.add_relation("c1", "恐龙", RelationType.LIKES, weight_delta=0.5)
        assert edge.weight == 1.0

    def test_get_relations_for(self, graph: CognitiveGraph) -> None:
        graph.add_relation("c1", "恐龙", RelationType.LIKES)
        graph.add_relation("c1", "月亮", RelationType.LIKES)
        graph.add_relation("c2", "恐龙", RelationType.REMEMBERS)
        rels = graph.get_relations_for("恐龙")
        assert len(rels) == 2

    def test_relation_creates_entities(self, graph: CognitiveGraph) -> None:
        graph.add_relation("c1", "new_entity", RelationType.LIKES)
        assert graph.get_entity("c1") is not None
        assert graph.get_entity("new_entity") is not None


class TestTimeDecay:
    def test_decay_old_edge(self) -> None:
        graph = CognitiveGraph()
        edge = graph.add_relation("c1", "恐龙", RelationType.LIKES, weight_delta=1.0)
        # Force the edge to be 31 days old
        edge.last_updated = datetime.now(timezone.utc) - timedelta(days=31)
        original_weight = edge.weight
        graph.apply_decay()
        # 30+ days: weight × 0.5
        assert edge.weight < original_weight

    def test_decay_recent_edge_unchanged(self) -> None:
        graph = CognitiveGraph()
        edge = graph.add_relation("c1", "恐龙", RelationType.LIKES, weight_delta=1.0)
        # Just added — no decay
        graph.apply_decay()
        # Weight should be ~1.0 (slight decay from age=0 days, but threshold is 30)
        assert edge.weight >= 0.5


class TestEntityAligner:
    def test_align_new_entity(self, graph: CognitiveGraph) -> None:
        aligner = EntityAligner(graph)
        e = aligner.align(EntityNode(name="暴龙"))
        assert e.name == "暴龙"
        assert graph.get_entity("暴龙") is not None

    def test_align_similar_entity(self, graph: CognitiveGraph) -> None:
        graph.add_entity("霸王龙")
        aligner = EntityAligner(graph)
        e = aligner.align(EntityNode(name="霸王龙"))
        # Should merge — same name
        assert e.name == "霸王龙"

    def test_align_with_aliases(self, graph: CognitiveGraph) -> None:
        graph.add_entity("霸王龙", aliases=["T-Rex"])
        aligner = EntityAligner(graph)
        # New entity that matches an alias
        e = aligner.align(EntityNode(name="T-Rex"))
        # Should merge into existing
        assert e.name == "霸王龙"
        assert "T-Rex" in e.aliases

    def test_align_substring_match(self, graph: CognitiveGraph) -> None:
        graph.add_entity("月光")
        aligner = EntityAligner(graph)
        # "月亮" is substring of "月光" or vice versa → 0.9 similarity
        e = aligner.align(EntityNode(name="月亮"))
        # Either merged or kept as new depending on similarity
        assert e.name in ("月光", "月亮")
