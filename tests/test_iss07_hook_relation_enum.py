"""Tests for ISS-07: daytime hook uses RelationType enum, not string."""

from niki.memory.graph import RelationType
from niki.memory.hook import DaytimeMemoryHook
from niki.memory.long_term import LongTermMemory


class FallbackLLM:
    async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return {"content": "stub"}


class TestHookRelationType:
    async def test_hook_uses_relation_type_enum(self) -> None:
        """ISS-07: the old code passed `relation="remembers"` (a raw string),
        bypassing the enum. This silently created a relation whose
        `relation` was the string, not the enum value."""
        long_term = LongTermMemory()
        # Patch CognitiveGraph to capture add_relation calls

        # Patch CognitiveGraph to capture add_relation calls
        from niki.memory.graph import CognitiveGraph

        calls: list[tuple] = []

        original_add = CognitiveGraph.add_relation

        def capturing_add(self, source, target, relation, weight_delta=0.1):
            calls.append((source, target, relation, weight_delta))
            return original_add(self, source, target, relation, weight_delta)

        CognitiveGraph.add_relation = capturing_add
        try:
            hook = DaytimeMemoryHook(long_term, CognitiveGraph())
            await hook.process(
                child_id="c1",
                text="我养了一只小狗叫旺财",
            )
        finally:
            CognitiveGraph.add_relation = original_add

        # Must have used the enum, not a string
        assert calls, "no add_relation calls"
        for call in calls:
            source, target, relation, _ = call
            assert isinstance(relation, RelationType), (
                f"ISS-07: relation should be RelationType, got {type(relation).__name__}"
            )
            assert relation == RelationType.REMEMBERS
