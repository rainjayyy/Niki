"""Tests for the topic blacklist (PRD #41)."""


from niki.safety.topic_blacklist import TopicBlacklist


class TestTopicBlacklistBasics:
    """Basic CRUD on the blacklist."""

    def test_default_blacklist_has_prd_topics(self) -> None:
        """Default blacklist includes the topics PRD #41 calls out."""
        bl = TopicBlacklist()
        assert bl.is_blacklisted("死亡")
        assert bl.is_blacklisted("暴力")
        assert bl.is_blacklisted("毒品")

    def test_custom_topics(self) -> None:
        """A custom blacklist can be supplied."""
        bl = TopicBlacklist({"游戏", "糖果"})
        assert bl.is_blacklisted("游戏")
        assert not bl.is_blacklisted("暴力")

    def test_add_and_remove(self) -> None:
        """Topics can be added and removed at runtime."""
        bl = TopicBlacklist()
        bl.add("作业")
        assert bl.is_blacklisted("作业")
        bl.remove("作业")
        assert not bl.is_blacklisted("作业")

    def test_topics_returns_copy(self) -> None:
        """The topics property returns a copy, not the internal set."""
        bl = TopicBlacklist()
        topics = bl.topics
        topics.add("fake")
        assert not bl.is_blacklisted("fake")


class TestTopicBlacklistMatching:
    """Topic matching on real Chinese / English strings."""

    def test_match_chinese_topic(self) -> None:
        bl = TopicBlacklist({"暴力"})
        matches = bl.match("我看到了一个暴力的视频")
        assert len(matches) == 1
        assert matches[0].topic == "暴力"
        assert matches[0].matched_text == "暴力"

    def test_match_no_hit(self) -> None:
        bl = TopicBlacklist({"暴力"})
        matches = bl.match("今天天气真好")
        assert matches == []

    def test_match_multiple_topics(self) -> None:
        bl = TopicBlacklist({"死亡", "暴力"})
        text = "暴力的电影描述了死亡"
        matches = bl.match(text)
        topics = {m.topic for m in matches}
        assert topics == {"暴力", "死亡"}

    def test_match_empty_text(self) -> None:
        bl = TopicBlacklist({"暴力"})
        assert bl.match("") == []

    def test_match_english_case_insensitive(self) -> None:
        bl = TopicBlacklist({"kill"})
        matches = bl.match("I want to KILL the monster")
        assert len(matches) >= 1

    def test_match_returns_positions(self) -> None:
        bl = TopicBlacklist({"暴力"})
        text = "暴力 暴力"
        matches = bl.match(text)
        # Both occurrences should be reported (different positions)
        positions = sorted(m.start for m in matches)
        assert positions == [0, 3]
