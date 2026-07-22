"""Tests for long-term memory (vector store)."""

import pytest

from niki.memory.long_term import LongTermMemory, MemoryRecord, MemorySource


@pytest.fixture
def mem() -> LongTermMemory:
    return LongTermMemory()


class TestLongTermMemory:
    async def test_add_and_count(self, mem: LongTermMemory) -> None:
        record = MemoryRecord(
            id="r1",
            content="恐龙很厉害",
            source=MemorySource.DAYTIME_HOOK,
            child_id="c1",
        )
        await mem.add(record)
        assert await mem.count() == 1

    async def test_search_finds_record(self, mem: LongTermMemory) -> None:
        await mem.add(MemoryRecord(
            id="r1",
            content="我喜欢恐龙",
            source=MemorySource.DAYTIME_HOOK,
            child_id="c1",
        ))
        await mem.add(MemoryRecord(
            id="r2",
            content="天空是蓝色的",
            source=MemorySource.DAYTIME_HOOK,
            child_id="c1",
        ))
        results = await mem.search("恐龙", child_id="c1")
        assert any("恐龙" in r.content for r in results)

    async def test_search_filters_by_child(self, mem: LongTermMemory) -> None:
        # Use unique content for this test to avoid cross-test interference
        await mem.add(MemoryRecord(
            id="unique-c1", content="UNIQUE_FILTER_TEST_C1", source=MemorySource.DAYTIME_HOOK, child_id="c1",
        ))
        await mem.add(MemoryRecord(
            id="unique-c2", content="UNIQUE_FILTER_TEST_C2", source=MemorySource.DAYTIME_HOOK, child_id="c2",
        ))
        results = await mem.search("UNIQUE_FILTER_TEST", child_id="c1")
        # All returned results must be for c1
        assert all(r.child_id == "c1" for r in results)
        assert any(r.id == "unique-c1" for r in results)
        assert not any(r.id == "unique-c2" for r in results)

    async def test_count_by_child(self, mem: LongTermMemory) -> None:
        for _ in range(3):
            await mem.add(MemoryRecord(
                id="x", content="x", source=MemorySource.DAYTIME_HOOK, child_id="c1",
            ))
        for _ in range(2):
            await mem.add(MemoryRecord(
                id="y", content="y", source=MemorySource.DAYTIME_HOOK, child_id="c2",
            ))
        assert await mem.count("c1") == 3
        assert await mem.count("c2") == 2
        assert await mem.count() == 5

    async def test_clear_for_child(self, mem: LongTermMemory) -> None:
        for i in range(3):
            await mem.add(MemoryRecord(
                id=f"r{i}",
                content=f"x{i}",
                source=MemorySource.DAYTIME_HOOK,
                child_id="c1",
            ))
        for i in range(2):
            await mem.add(MemoryRecord(
                id=f"y{i}",
                content=f"y{i}",
                source=MemorySource.DAYTIME_HOOK,
                child_id="c2",
            ))
        deleted = await mem.clear_for_child("c1")
        assert deleted == 3
        assert await mem.count("c1") == 0
        assert await mem.count("c2") == 2

    def test_make_id_deterministic(self) -> None:
        id1 = LongTermMemory.make_id("c1", "hello", "daytime_hook")
        id2 = LongTermMemory.make_id("c1", "hello", "daytime_hook")
        assert id1 == id2

    def test_make_id_unique(self) -> None:
        id1 = LongTermMemory.make_id("c1", "hello", "daytime_hook")
        id2 = LongTermMemory.make_id("c2", "hello", "daytime_hook")
        assert id1 != id2


class TestMemoryRecord:
    def test_to_dict(self) -> None:
        record = MemoryRecord(
            id="r1",
            content="test",
            source=MemorySource.DAYTIME_HOOK,
            child_id="c1",
            emotion="happy",
            intensity=4,
        )
        d = record.to_dict()
        assert d["id"] == "r1"
        assert d["content"] == "test"
        assert d["source"] == "daytime_hook"
        assert d["emotion"] == "happy"
        assert d["intensity"] == 4
