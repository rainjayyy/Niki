"""Family blacklist — PRD #32.

Per-family topic blacklist (parent-managed). Differs from the
global TopicBlacklist in #1 (safety) which is system-wide.

PRD #32: 家长可以设置话题黑名单（如"打游戏"），Niki遇到这些话题时自动降级处理而非生硬拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class BlacklistAction(StrEnum):
    """What to do when a blacklisted topic is detected."""

    DOWNGRADE = "downgrade"  # Gently deflect (PRD: 降级处理)
    REDIRECT = "redirect"  # Redirect to related safe content
    APOLOGIZE = "apologize"  # Politely decline


@dataclass
class BlacklistMatch:
    """A topic that matched the family blacklist."""

    topic: str
    action: BlacklistAction
    matched_text: str
    position: tuple[int, int]
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    added_by: str = ""  # parent_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "action": self.action.value,
            "matched_text": self.matched_text,
            "position": list(self.position),
            "added_at": self.added_at.isoformat(),
            "added_by": self.added_by,
        }


@dataclass
class BlacklistedTopic:
    """A single entry in the family blacklist."""

    topic: str
    action: BlacklistAction = BlacklistAction.DOWNGRADE
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    added_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "action": self.action.value,
            "added_at": self.added_at.isoformat(),
            "added_by": self.added_by,
        }


class FamilyBlacklist:
    """Per-family topic blacklist with action assignment.

    Unlike the global TopicBlacklist (system safety), this is parent-
    managed: each family can add their own topics (e.g. "video games",
    "competitions", "specific scary shows") and pick the action.

    When a topic is detected:
    - DOWNGRADE: gently deflect (e.g. "我不太了解这个呢~")
    - REDIRECT: pivot to related safe content
    - APOLOGIZE: politely decline
    """

    def __init__(self) -> None:
        self._entries: dict[str, BlacklistedTopic] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    def add(
        self,
        topic: str,
        action: BlacklistAction = BlacklistAction.DOWNGRADE,
        added_by: str = "",
    ) -> BlacklistedTopic:
        """Add or update a blacklisted topic.

        Args:
            topic: The topic to blacklist.
            action: What to do when detected.
            added_by: The parent who added this entry (for audit).

        Returns:
            The BlacklistedTopic record.
        """
        if not topic or not topic.strip():
            raise ValueError("topic must be non-empty")

        entry = BlacklistedTopic(
            topic=topic,
            action=action,
            added_by=added_by,
        )
        self._entries[topic] = entry
        logger.info(
            "blacklist.added",
            topic=topic,
            action=action.value,
            added_by=added_by,
        )
        return entry

    def remove(self, topic: str) -> bool:
        """Remove a topic from the blacklist.

        Returns True if the topic was removed, False if it wasn't there.
        """
        if topic in self._entries:
            del self._entries[topic]
            logger.info("blacklist.removed", topic=topic)
            return True
        return False

    def get(self, topic: str) -> BlacklistedTopic | None:
        return self._entries.get(topic)

    def list_topics(self) -> list[BlacklistedTopic]:
        """List all blacklisted topics."""
        return list(self._entries.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "topics": [e.to_dict() for e in self._entries.values()],
        }

    def match(self, text: str) -> list[BlacklistMatch]:
        """Find all blacklist matches in the given text.

        Args:
            text: The text to scan.

        Returns:
            A list of BlacklistMatch objects.
        """
        if not text or not self._entries:
            return []

        matches: list[BlacklistMatch] = []

        for entry in self._entries.values():
            topic = entry.topic
            # Find ALL matches (deduplicated by position)
            seen_positions: set[int] = set()
            for haystack in [text, text.lower()]:
                start = 0
                while True:
                    idx = haystack.find(topic, start)
                    if idx == -1:
                        break
                    if idx not in seen_positions:
                        seen_positions.add(idx)
                        matches.append(
                            BlacklistMatch(
                                topic=entry.topic,
                                action=entry.action,
                                matched_text=text[idx : idx + len(topic)],
                                position=(idx, idx + len(topic)),
                                added_at=entry.added_at,
                                added_by=entry.added_by,
                            )
                        )
                    start = idx + len(topic)

        logger.debug(
            "blacklist.match",
            text_length=len(text),
            matches_found=len(matches),
        )
        return matches
