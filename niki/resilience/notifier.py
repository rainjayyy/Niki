"""Degradation notifier — PRD #60.

Sends a silent signal to the parent dashboard when the LLM is degraded.
PRD: 静默感知 — parent sees a soft message ("Niki 正在做一个小小的内部调整"),
NOT a panic alert, NOT a push notification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)


# PRD #60: silent message (positive, reassuring tone)
DEGRADATION_MESSAGE = (
    "Niki 正在做一个小小的内部调整，功能正常但回答可能会简单一些，请放心使用~"
)


@dataclass
class DegradationState:
    """Tracks current degradation status."""

    active: bool = False
    started_at: datetime | None = None
    reason: str = ""
    notification_count: int = 0

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "reason": self.reason,
            "notification_count": self.notification_count,
            "message": DEGRADATION_MESSAGE,
        }


class DegradationNotifier:
    """Tracks degradation state + prepares dashboard signal.

    In production, this would push to:
    - Parent dashboard banner (silent)
    - Internal monitoring (Datadog/Sentry)
    - Optional log alert for ops

    Here: just maintains state and logs.
    """

    def __init__(self) -> None:
        self._state = DegradationState()

    @property
    def state(self) -> DegradationState:
        return self._state

    async def notify_active(self, reason: str) -> None:
        """Mark degradation as active + record notification."""
        if not self._state.active:
            self._state.active = True
            self._state.started_at = datetime.utcnow()
            self._state.reason = reason
        self._state.notification_count += 1
        logger.info(
            "degradation.active",
            reason=reason,
            count=self._state.notification_count,
        )

    async def notify_recovered(self) -> None:
        """Mark degradation as resolved (when breaker closes)."""
        if self._state.active:
            duration = (
                (datetime.utcnow() - self._state.started_at).total_seconds()
                if self._state.started_at
                else 0
            )
            logger.info(
                "degradation.recovered",
                duration_s=duration,
                notifications=self._state.notification_count,
            )
            self._state = DegradationState()

    def is_active(self) -> bool:
        return self._state.active

    def get_message(self) -> str:
        """Get the silent dashboard message (PRD #60)."""
        return DEGRADATION_MESSAGE
