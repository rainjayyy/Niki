"""Tests for short-term memory."""

import uuid

import pytest

from niki.memory.short_term import ConversationTurn, ShortTermMemory


@pytest.fixture
def mem() -> ShortTermMemory:
    return ShortTermMemory(max_rounds=5)


def _unique_session_id() -> str:
    """Generate a unique session ID per test invocation.

    Tests share a Redis instance (real or in-memory), so using static
    session IDs leads to cross-test interference.
    """
    return f"test-{uuid.uuid4().hex[:16]}"


class TestShortTermMemory:
    async def test_empty_returns_no_turns(self, mem: ShortTermMemory) -> None:
        turns = await mem.get_recent(_unique_session_id())
        assert turns == []

    async def test_append_adds_turn(self, mem: ShortTermMemory) -> None:
        sid = _unique_session_id()
        turn = ConversationTurn(role="child", text="你好")
        turns = await mem.append(sid, turn)
        assert len(turns) == 1
        assert turns[0].text == "你好"

    async def test_trim_to_max_rounds(self, mem: ShortTermMemory) -> None:
        """When more than max_rounds turns, oldest are dropped."""
        sid = _unique_session_id()
        for i in range(10):
            await mem.append(sid, ConversationTurn(role="child", text=f"msg-{i}"))
        turns = await mem.get_recent(sid)
        assert len(turns) == 5
        # Should keep the LAST 5
        assert turns[0].text == "msg-5"
        assert turns[-1].text == "msg-9"

    async def test_separate_sessions(self, mem: ShortTermMemory) -> None:
        sid1 = _unique_session_id()
        sid2 = _unique_session_id()
        await mem.append(sid1, ConversationTurn(role="child", text="session 1"))
        await mem.append(sid2, ConversationTurn(role="child", text="session 2"))
        turns1 = await mem.get_recent(sid1)
        turns2 = await mem.get_recent(sid2)
        assert turns1[0].text == "session 1"
        assert turns2[0].text == "session 2"

    async def test_clear_session(self, mem: ShortTermMemory) -> None:
        sid = _unique_session_id()
        await mem.append(sid, ConversationTurn(role="child", text="hello"))
        await mem.clear(sid)
        turns = await mem.get_recent(sid)
        assert turns == []

    async def test_role_preserved(self, mem: ShortTermMemory) -> None:
        sid = _unique_session_id()
        await mem.append(sid, ConversationTurn(role="niki", text="hi back"))
        turns = await mem.get_recent(sid)
        assert turns[0].role == "niki"

    async def test_intent_preserved(self, mem: ShortTermMemory) -> None:
        sid = _unique_session_id()
        await mem.append(
            sid,
            ConversationTurn(role="child", text="恐龙", intent="encyclopedia"),
        )
        turns = await mem.get_recent(sid)
        assert turns[0].intent == "encyclopedia"

    async def test_clear_all(self, mem: ShortTermMemory) -> None:
        # Use unique session IDs to avoid test interference
        s1 = _unique_session_id()
        s2 = _unique_session_id()
        await mem.append(s1, ConversationTurn(role="child", text="1"))
        await mem.append(s2, ConversationTurn(role="child", text="2"))
        await mem.clear_all()
        # After clear_all, in-memory cache is wiped, and Redis keys are deleted
        # for THIS instance's tracked sessions. We just check that the
        # in-memory cache for s1 is empty.
        assert s1 not in mem._cache
        assert s2 not in mem._cache


class TestConversationTurn:
    def test_to_dict(self) -> None:
        turn = ConversationTurn(role="child", text="hello", intent="greeting")
        d = turn.to_dict()
        assert d["role"] == "child"
        assert d["text"] == "hello"
        assert d["intent"] == "greeting"
        assert "timestamp" in d

    def test_from_dict(self) -> None:
        turn = ConversationTurn(role="child", text="hello")
        d = turn.to_dict()
        turn2 = ConversationTurn.from_dict(d)
        assert turn2.role == turn.role
        assert turn2.text == turn.text
