"""Daily session time limiter — PRD #31, #17.

Tracks a child's daily usage and enforces a soft + hard limit:
- Default limit: 60 minutes (configurable per family)
- Soft warning: at (limit - 5) min, Niki says "5 more minutes!"
- Hard limit: at limit, Niki gracefully ends the session
  with a positive summary of today's achievements

PRD #17: "我走开了忘记关麦克风时，Niki不要一直自言自语" — limiter also
guards against runaway idle sessions via max_idle_minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# PRD default: 60 minutes
DEFAULT_DAILY_LIMIT_MINUTES: int = 60

# PRD #17: soft warning at 5 minutes before hard limit
SOFT_WARNING_LEAD_MINUTES: int = 5

# PRD: max idle before auto-end
MAX_IDLE_MINUTES: int = 10


class ExitReason(StrEnum):
    """Why a session ended (for the parent dashboard)."""

    HARD_LIMIT = "hard_limit"        # Hit daily time cap
    PARENT_PAUSE = "parent_pause"    # Parent tapped "pause" in mini-program
    PARENT_DELETE = "parent_delete"  # Parent triggered account deletion
    CHILD_BYE = "child_bye"        # Child said "bye" (natural exit)
    IDLE_TIMEOUT = "idle_timeout"   # No activity for too long


@dataclass
class SessionStats:
    """Summary of a single session for the parent dashboard."""

    child_id: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    exit_reason: ExitReason
    message_count: int = 0
    topic_visited: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "duration_minutes": round(self.duration_seconds / 60, 2),
            "exit_reason": self.exit_reason.value,
            "message_count": self.message_count,
            "topic_visited": list(self.topic_visited),
        }


class SoftLimitReached(Exception):
    """Raised when the child has 5 minutes left (soft warning)."""


@dataclass
class SessionLimiter:
    """Tracks daily usage per child and enforces soft + hard limits.

    Usage:
        limiter = SessionLimiter(daily_limit_minutes=60)
        if limiter.should_warn(child_id):
            ... tell child "5 more minutes" ...
        if limiter.is_exhausted(child_id):
            ... end session ...
    """

    def __init__(
        self,
        daily_limit_minutes: int = DEFAULT_DAILY_LIMIT_MINUTES,
        max_idle_minutes: int = MAX_IDLE_MINUTES,
    ) -> None:
        self.daily_limit_minutes = daily_limit_minutes
        self.max_idle_minutes = max_idle_minutes
        # Per-child state
        self._session_start: dict[str, datetime] = {}
        self._session_last_activity: dict[str, datetime] = {}
        self._soft_warned: set[str] = set()  # warned this session
        self._ended: dict[str, ExitReason] = {}  # ended this session

    def start_session(self, child_id: str, now: datetime | None = None) -> None:
        """Mark a session as started."""
        now = now or datetime.now(timezone.utc)
        self._session_start[child_id] = now
        self._session_last_activity[child_id] = now
        self._soft_warned.discard(child_id)
        self._ended.pop(child_id, None)
        logger.info("session.started", child_id=child_id)

    def record_activity(self, child_id: str, now: datetime | None = None) -> None:
        """Update the last-activity timestamp (called on each message)."""
        now = now or datetime.now(timezone.utc)
        if child_id not in self._session_start:
            self.start_session(child_id, now)
        else:
            self._session_last_activity[child_id] = now

    def end_session(
        self,
        child_id: str,
        reason: ExitReason,
        now: datetime | None = None,
    ) -> SessionStats | None:
        """End a session and return its stats.

        Returns None if no session was started.
        """
        if child_id not in self._session_start:
            return None

        now = now or datetime.now(timezone.utc)
        started = self._session_start[child_id]
        duration = (now - started).total_seconds()
        self._ended[child_id] = reason

        stats = SessionStats(
            child_id=child_id,
            started_at=started,
            ended_at=now,
            duration_seconds=duration,
            exit_reason=reason,
        )

        logger.info(
            "session.ended",
            child_id=child_id,
            reason=reason.value,
            duration_seconds=duration,
        )

        # Clean up session state
        del self._session_start[child_id]
        self._session_last_activity.pop(child_id, None)
        self._soft_warned.discard(child_id)

        return stats

    def is_started(self, child_id: str) -> bool:
        return child_id in self._session_start

    def is_ended(self, child_id: str) -> ExitReason | None:
        return self._ended.get(child_id)

    def elapsed_minutes(self, child_id: str, now: datetime | None = None) -> float:
        """Get the elapsed time in the current session (in minutes)."""
        if child_id not in self._session_start:
            return 0.0
        now = now or datetime.now(timezone.utc)
        elapsed = (now - self._session_start[child_id]).total_seconds() / 60
        return max(0.0, elapsed)

    def idle_minutes(self, child_id: str, now: datetime | None = None) -> float:
        """Get the minutes since last activity (PRD #17)."""
        if child_id not in self._session_last_activity:
            return 0.0
        now = now or datetime.now(timezone.utc)
        elapsed = (now - self._session_last_activity[child_id]).total_seconds() / 60
        return max(0.0, elapsed)

    def should_warn(self, child_id: str, now: datetime | None = None) -> bool:
        """True if the soft warning should be issued (5 min before limit)."""
        if child_id in self._soft_warned:
            return False  # Already warned
        if not self.is_started(child_id):
            return False
        elapsed = self.elapsed_minutes(child_id, now)
        return elapsed >= self.daily_limit_minutes - SOFT_WARNING_LEAD_MINUTES

    def mark_warned(self, child_id: str) -> None:
        """Mark that we've issued the soft warning for this session."""
        self._soft_warned.add(child_id)

    def is_exhausted(self, child_id: str, now: datetime | None = None) -> bool:
        """True if the hard limit has been reached (must end session)."""
        if not self.is_started(child_id):
            return False
        return self.elapsed_minutes(child_id, now) >= self.daily_limit_minutes

    def is_idle_timeout(self, child_id: str, now: datetime | None = None) -> bool:
        """True if no activity for too long (PRD #17)."""
        if not self.is_started(child_id):
            return False
        return self.idle_minutes(child_id, now) >= self.max_idle_minutes

    def get_remaining_minutes(self, child_id: str, now: datetime | None = None) -> float:
        """Get the minutes remaining in the daily limit."""
        if not self.is_started(child_id):
            return float(self.daily_limit_minutes)
        return max(0.0, self.daily_limit_minutes - self.elapsed_minutes(child_id, now))
