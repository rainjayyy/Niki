"""Tests for the daytime memory hook."""

import pytest

from niki.memory.graph import CognitiveGraph
from niki.memory.hook import DaytimeMemoryHook, ExtractionKind
from niki.memory.long_term import LongTermMemory


@pytest.fixture
def hook() -> DaytimeMemoryHook:
    long_term = LongTermMemory()
    graph = CognitiveGraph()
    return DaytimeMemoryHook(long_term, graph)


class TestEntityExtraction:
    async def test_extract_pet_name(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "我的小狗叫旺财")
        assert any(c.kind == ExtractionKind.NEW_ENTITY for c in candidates)
        texts = [c.text for c in candidates]
        assert "旺财" in texts

    async def test_extract_preference(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "我喜欢恐龙")
        texts = [c.text for c in candidates]
        assert "恐龙" in texts

    async def test_extract_fear(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "我害怕黑暗")
        texts = [c.text for c in candidates]
        assert "黑暗" in texts


class TestEmotionExtraction:
    async def test_extract_sad(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "我今天很难过")
        emotion_candidates = [c for c in candidates if c.kind == ExtractionKind.NEW_EMOTION]
        assert len(emotion_candidates) >= 1
        assert emotion_candidates[0].text == "难过"

    async def test_extract_happy(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "我今天非常开心")
        emotion_candidates = [c for c in candidates if c.kind == ExtractionKind.NEW_EMOTION]
        assert any(c.text == "开心" for c in emotion_candidates)


class TestHookSideEffects:
    async def test_hook_adds_to_graph(self, hook: DaytimeMemoryHook) -> None:
        await hook.process("c1", "我的小狗叫旺财")
        # Entity should be in the graph
        assert hook._graph.get_entity("旺财") is not None

    async def test_hook_adds_to_long_term(self, hook: DaytimeMemoryHook) -> None:
        await hook.process("c1", "我的小狗叫旺财")
        # Long-term memory should have a record
        assert await hook._long_term.count() >= 1

    async def test_hook_creates_remembers_relation(self, hook: DaytimeMemoryHook) -> None:
        await hook.process("c1", "我的小狗叫旺财")
        rels = hook._graph.get_relations_for("旺财")
        assert any(r.source == "c1" for r in rels)


class TestEmptyInput:
    async def test_empty_text(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "")
        assert candidates == []

    async def test_no_match(self, hook: DaytimeMemoryHook) -> None:
        candidates = await hook.process("c1", "今天天气真好啊")
        # No entities or emotions expected
        assert candidates == []


class TestHookRelationEnumISS07:
    """ISS-07 regression: hook.process() must pass a RelationType
    enum (not a str) to CognitiveGraph.add_relation. The original
    code passed the literal string ``"remembers"`` which mypy
    flagged and which could cause structural-log / Neo4j-write
    bugs downstream when relation.value is consumed."""

    async def test_relation_is_enum_type(self, hook: DaytimeMemoryHook) -> None:
        """The relation stored in the graph must be a RelationType
        enum member — not a plain str — so Neo4j / structured log
        consumers see the .value discriminator uniformly."""
        from niki.memory.graph import RelationType

        await hook.process("c1", "我的小狗叫旺财")
        rels = hook._graph.get_relations_for("旺财")
        assert rels, "expected at least one relation for 旺财"
        # The relation type must be a RelationType enum member, not a str.
        rel = rels[0]
        assert isinstance(rel.relation, RelationType), (
            f"ISS-07 regression: relation is {type(rel.relation).__name__}, "
            f"expected RelationType enum. Value={rel.relation!r}"
        )
        assert rel.relation == RelationType.REMEMBERS
        # Belt-and-braces — the enum value still stringifies to
        # "remembers" so persisted storage stays compatible.
        assert rel.relation.value == "remembers"
