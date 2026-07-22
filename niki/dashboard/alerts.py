"""Alerts — PRD #27, #28, #29.

Two types of alerts:
- SafetyValveAlert: tutoring safety-valve was triggered (parent sees the
  problem + a 'may need attention' suggestion)
- HighRiskAlert: child expressed high-risk signal (self-harm, etc.)
  → IMMEDIATELY pushed to parent with full text (PRD #29)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class AlertSeverity(StrEnum):
    """How urgent is this alert."""

    INFO = "info"                # Routine event (safety-valve)
    URGENT = "urgent"            # Requires attention (high-risk signal)


@dataclass
class SafetyValveAlert:
    """Generated when a child triggers the tutoring safety valve (PRD #27).

    The parent sees:
    - The problem the child was stuck on
    - That the agent had to give the answer
    - A 'may need attention' suggestion
    """

    id: str
    child_id: str
    severity: AlertSeverity = AlertSeverity.INFO
    problem_summary: str = ""         # e.g. "数学应用题"
    round_reached: int = 4
    triggered_at: datetime = field(default_factory=datetime.utcnow)
    action_suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "child_id": self.child_id,
            "severity": self.severity.value,
            "problem_summary": self.problem_summary,
            "round_reached": self.round_reached,
            "triggered_at": self.triggered_at.isoformat(),
            "action_suggestion": self.action_suggestion,
        }


@dataclass
class HighRiskAlert:
    """Generated when a child expresses a high-risk signal (PRD #29).

    IMMEDIATELY pushed to parent with the original text + the fact
    that Niki already responded with a caring message.
    """

    id: str
    child_id: str
    original_text: str            # PRD #29: 看到原文
    niki_response_summary: str
    severity: AlertSeverity = AlertSeverity.URGENT
    triggered_at: datetime = field(default_factory=datetime.utcnow)
    delivered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "child_id": self.child_id,
            "severity": self.severity.value,
            "original_text": self.original_text,
            "niki_response_summary": self.niki_response_summary,
            "triggered_at": self.triggered_at.isoformat(),
            "delivered": self.delivered,
        }
