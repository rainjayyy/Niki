"""Remote pause controller — PRD #33.

Parent-initiated pause via the mini-program:
- One-tap pause from the parent's phone
- Niki says a natural goodbye so the child doesn't know
  the parent intervened (PRD: "自然告别不让孩子知道我介入了")
- The session ends with a positive summary

PRD #23: "我今天很开心呢" → natural exit should celebrate the day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from niki.session.limiter import ExitReason, SessionLimiter, SessionStats

logger = structlog.get_logger(__name__)


# PRD #23: positive exit messages (child-facing)
NATURAL_EXIT_MESSAGES: list[str] = [
    "今天和你玩得真开心~ Niki学到了好多新东西呢！",
    "哇，今天我们一起探索了好多有趣的事情~ 明天见啦！",
    "今天有你陪着 Niki 好开心呀！早点休息哦~",
    "今天聊得很开心~ Niki 期待下次和你一起玩！",
]

# PRD: simpler message for parent-pause (no "you" reference, no summary)
PARENT_PAUSE_MESSAGES: list[str] = [
    "嗯~ Niki 有点累了，今天就先聊到这里吧~ 明天 Niki 再来陪你玩！",
    "今天就到这儿啦~ Niki 休息一下，一会儿见哦~",
    "好啦，Niki 准备休息啦~ 明天继续和你一起探索！",
]

# Parent-pause message (must NOT make the child think parent did it)
PARENT_PAUSE_DEFAULT = PARENT_PAUSE_MESSAGES[0]


@dataclass
class NaturalExitMessage:
    """A friendly exit message + summary for the child."""

    text: str
    show_summary: bool = True
    summary_topics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "show_summary": self.show_summary,
            "summary_topics": list(self.summary_topics),
        }


class RemotePauseController:
    """Handles parent-initiated pause requests from the mini-program.

    The child should never know that the parent intervened. The
    pause message sounds like Niki decided to take a break.
    """

    def __init__(self, limiter: SessionLimiter | None = None) -> None:
        self._limiter = limiter or SessionLimiter()
        # Pending pause requests (parent_id → pause info)
        self._pending_pauses: dict[str, dict[str, Any]] = {}
        # Sessions that were paused by parent
        self._paused_sessions: dict[str, datetime] = {}

    @property
    def limiter(self) -> SessionLimiter:
        return self._limiter

    def request_pause(
        self,
        child_id: str,
        parent_id: str,
        reason: str = "parent_initiated",
    ) -> None:
        """Record a pause request from the parent (one-tap in mini-program).

        The request is queued; the next time the child sends a message,
        Niki will end the session with a natural exit message.
        """
        self._pending_pauses[child_id] = {
            "parent_id": parent_id,
            "reason": reason,
            "requested_at": datetime.utcnow().isoformat(),
        }
        logger.info(
            "session.parent_pause_requested",
            child_id=child_id,
            parent_id=parent_id,
            reason=reason,
        )

    def has_pending_pause(self, child_id: str) -> bool:
        """True if a parent pause is pending (will apply on next activity)."""
        return child_id in self._pending_pauses

    def consume_pause(self, child_id: str) -> NaturalExitMessage | None:
        """Consume a pending pause (returns natural exit message).

        Returns None if no pause is pending.
        """
        if child_id not in self._pending_pauses:
            return None

        pause = self._pending_pauses.pop(child_id)
        self._paused_sessions[child_id] = datetime.utcnow()

        # Use a pause message — should sound like Niki is tired
        msg = PARENT_PAUSE_DEFAULT
        logger.info(
            "session.parent_pause_consumed",
            child_id=child_id,
            parent_id=pause["parent_id"],
        )
        return NaturalExitMessage(text=msg, show_summary=False)

    def end_session_by_parent(
        self,
        child_id: str,
        message_count: int = 0,
        topics_visited: list[str] | None = None,
    ) -> SessionStats | None:
        """End the session as a parent-pause.

        Returns:
            SessionStats if the session existed, else None.
        """
        return self._limiter.end_session(
            child_id,
            reason=ExitReason.PARENT_PAUSE,
        )

    def build_natural_exit(
        self,
        child_id: str,
        topics_visited: list[str] | None = None,
        show_summary: bool = True,
    ) -> NaturalExitMessage:
        """Build a natural exit message + summary for the child.

        Used when:
        - The child says "bye" (natural)
        - The hard daily limit is reached
        """
        # Pick a random-ish message (use len to rotate)
        idx = len(topics_visited or []) % len(NATURAL_EXIT_MESSAGES)
        text = NATURAL_EXIT_MESSAGES[idx]

        # Build the "today's summary"
        summary_topics = topics_visited or []
        return NaturalExitMessage(
            text=text,
            show_summary=show_summary,
            summary_topics=summary_topics,
        )

    def is_paused(self, child_id: str) -> bool:
        """True if this child has been paused (any reason)."""
        return child_id in self._paused_sessions

    def clear_pause(self, child_id: str) -> None:
        """Clear the pause state (used when parent resumes)."""
        self._paused_sessions.pop(child_id, None)
        self._pending_pauses.pop(child_id, None)
        logger.info("session.pause_cleared", child_id=child_id)
