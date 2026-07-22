"""Topic blacklist — PRD #41 'downgrade' strategy.

Parents may mark sensitive topics (e.g. 'death', 'violence'). When the
child's input mentions these topics, the system DOWNGRADES the request
rather than blocking it — for example, routing to the Emotion Agent in
empathic mode instead of refusing to answer.

This is intentionally different from a hard block: the child is not
told "this topic is forbidden". They get a gentle, supportive deflection.
"""

from __future__ import annotations

import structlog

from niki.safety.dictionaries import DEFAULT_BLACKLISTED_TOPICS

logger = structlog.get_logger(__name__)


def _is_cjk(s: str) -> bool:
    """True if any character in s is CJK (Chinese / Japanese kanji)."""
    return any("一" <= c <= "鿿" for c in s)


class TopicMatch:
    """A topic that matched the blacklist."""

    __slots__ = ("topic", "matched_text", "start", "end")

    def __init__(self, topic: str, matched_text: str, start: int, end: int) -> None:
        self.topic = topic
        self.matched_text = matched_text
        self.start = start
        self.end = end

    def __repr__(self) -> str:
        return f"TopicMatch(topic={self.topic!r}, text={self.matched_text!r})"


class TopicBlacklist:
    """Manages a per-family set of blacklisted topics and matches them.

    Usage:
        blacklist = TopicBlacklist()                  # default topics
        blacklist = TopicBlacklist({"violence"})      # custom topics
        matches = blacklist.match("我家的仓鼠死了")  # finds "死"
        if matches:
            # downgrade to emotion agent
    """

    def __init__(self, topics: set[str] | None = None) -> None:
        # Default to PRD-suggested blacklist; parents may override
        self._topics = topics if topics is not None else set(DEFAULT_BLACKLISTED_TOPICS)

    @property
    def topics(self) -> set[str]:
        """Return a copy of the current blacklist."""
        return set(self._topics)

    def add(self, topic: str) -> None:
        """Add a topic to the blacklist."""
        self._topics.add(topic)

    def remove(self, topic: str) -> None:
        """Remove a topic from the blacklist."""
        self._topics.discard(topic)

    def is_blacklisted(self, topic: str) -> bool:
        """Check whether a single topic is blacklisted."""
        return topic in self._topics

    def match(self, text: str) -> list[TopicMatch]:
        """Find all blacklisted topics in the given text.

        Matches are case-insensitive for English, exact for Chinese.
        All occurrences are reported (not just the first).

        Args:
            text: The input text to scan.

        Returns:
            A list of TopicMatch instances (empty if no match).
        """
        if not text:
            return []

        # Pre-compute lowercase text only if any non-CJK topic exists
        has_non_cjk = any(not _is_cjk(t) for t in self._topics)
        text_lower = text.lower() if has_non_cjk else text

        matches: list[TopicMatch] = []
        seen: set[tuple[str, int]] = set()

        for topic in self._topics:
            topic_is_cjk = _is_cjk(topic)
            haystack = text if topic_is_cjk else text_lower
            needle = topic if topic_is_cjk else topic.lower()

            start = 0
            while True:
                idx = haystack.find(needle, start)
                if idx == -1:
                    break
                key = (topic, idx)
                if key in seen:
                    start = idx + len(needle)
                    continue
                seen.add(key)
                matches.append(
                    TopicMatch(
                        topic=topic,
                        matched_text=text[idx : idx + len(needle)],
                        start=idx,
                        end=idx + len(needle),
                    )
                )
                start = idx + len(needle)

        logger.debug(
            "topic_blacklist.match",
            text_length=len(text),
            matches_found=len(matches),
        )

        return matches
