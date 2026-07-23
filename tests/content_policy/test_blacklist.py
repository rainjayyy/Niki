"""Tests for the family blacklist."""

import pytest

from niki.content_policy.blacklist import (
    BlacklistAction,
    FamilyBlacklist,
)


@pytest.fixture
def blacklist() -> FamilyBlacklist:
    return FamilyBlacklist()


class TestBlacklistCRUD:
    def test_empty(self, blacklist: FamilyBlacklist) -> None:
        assert blacklist.size == 0
        assert blacklist.list_topics() == []

    def test_add_topic(self, blacklist: FamilyBlacklist) -> None:
        entry = blacklist.add("打游戏")
        assert entry.topic == "打游戏"
        assert entry.action == BlacklistAction.DOWNGRADE
        assert blacklist.size == 1

    def test_add_with_action(self, blacklist: FamilyBlacklist) -> None:
        entry = blacklist.add("恐怖片", BlacklistAction.REDIRECT)
        assert entry.action == BlacklistAction.REDIRECT

    def test_add_with_added_by(self, blacklist: FamilyBlacklist) -> None:
        entry = blacklist.add("打游戏", added_by="parent-1")
        assert entry.added_by == "parent-1"

    def test_add_duplicate_updates(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("打游戏")
        blacklist.add("打游戏", BlacklistAction.REDIRECT)
        # Should update, not duplicate
        assert blacklist.size == 1
        assert blacklist.get("打游戏").action == BlacklistAction.REDIRECT

    def test_remove_existing(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("打游戏")
        removed = blacklist.remove("打游戏")
        assert removed is True
        assert blacklist.size == 0

    def test_remove_nonexistent(self, blacklist: FamilyBlacklist) -> None:
        removed = blacklist.remove("nope")
        assert removed is False

    def test_get_topic(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("打游戏", BlacklistAction.APOLOGIZE)
        entry = blacklist.get("打游戏")
        assert entry is not None
        assert entry.action == BlacklistAction.APOLOGIZE

    def test_get_nonexistent(self, blacklist: FamilyBlacklist) -> None:
        assert blacklist.get("nope") is None


class TestBlacklistMatch:
    def test_no_match_empty_blacklist(self, blacklist: FamilyBlacklist) -> None:
        assert blacklist.match("我想打游戏") == []

    def test_match_chinese(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("打游戏")
        matches = blacklist.match("我今天想打游戏")
        assert len(matches) == 1
        assert matches[0].topic == "打游戏"

    def test_match_english(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("video games")
        matches = blacklist.match("let's play video games")
        assert len(matches) == 1

    def test_match_case_insensitive(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("SCARY")
        matches = blacklist.match("this is SCARY content")
        assert len(matches) == 1

    def test_multiple_matches(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("game")
        blacklist.add("movie")
        matches = blacklist.match("the game is like a movie")
        assert len(matches) == 2

    def test_action_in_match(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("test", BlacklistAction.APOLOGIZE)
        matches = blacklist.match("this is a test")
        assert matches[0].action == BlacklistAction.APOLOGIZE

    def test_to_dict(self, blacklist: FamilyBlacklist) -> None:
        entry = blacklist.add("test", added_by="p1")
        d = entry.to_dict()
        assert d["topic"] == "test"
        assert d["action"] == "downgrade"
        assert d["added_by"] == "p1"


class TestEmptyTopic:
    def test_add_empty_raises(self, blacklist: FamilyBlacklist) -> None:
        with pytest.raises(ValueError):
            blacklist.add("")
        with pytest.raises(ValueError):
            blacklist.add("   ")


class TestListTopics:
    def test_list_returns_all(self, blacklist: FamilyBlacklist) -> None:
        for t in ["a", "b", "c"]:
            blacklist.add(t)
        topics = blacklist.list_topics()
        assert len(topics) == 3
        assert {t.topic for t in topics} == {"a", "b", "c"}


class TestToDict:
    def test_to_dict_empty(self, blacklist: FamilyBlacklist) -> None:
        d = blacklist.to_dict()
        assert d["size"] == 0
        assert d["topics"] == []

    def test_to_dict_with_topics(self, blacklist: FamilyBlacklist) -> None:
        blacklist.add("a")
        blacklist.add("b")
        d = blacklist.to_dict()
        assert d["size"] == 2
        assert len(d["topics"]) == 2
