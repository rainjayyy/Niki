"""Session control — PRD #31, #33.

Public surface:
    SessionLimiter           — daily usage cap (default 60 min, configurable)
    SoftLimitReached         — raised 5 min before hard limit
    ExitReason               — why a session ended
    RemotePauseController    — parent-initiated pause (one-tap mini-program)
    NaturalExitMessage        — friendly goodbye with today's summary
"""

from niki.session.limiter import (
    DEFAULT_DAILY_LIMIT_MINUTES,
    ExitReason,
    SessionLimiter,
    SessionStats,
    SoftLimitReached,
)
from niki.session.pause import (
    NaturalExitMessage,
    RemotePauseController,
)

__all__ = [
    "DEFAULT_DAILY_LIMIT_MINUTES",
    "SessionLimiter",
    "SessionStats",
    "SoftLimitReached",
    "ExitReason",
    "RemotePauseController",
    "NaturalExitMessage",
]
