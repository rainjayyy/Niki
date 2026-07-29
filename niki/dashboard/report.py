"""Emotion weekly report — PRD #24, #27.

Parent-facing weekly summary of the child's emotional state.
Design principles (PRD):
- 积极优先 (positive-first): lead with discoveries / wins
- 模糊化负向 (fuzzy negative): negative dips shown as action descriptions
  ("有 2 次聊完就好了😊") not raw numbers
- 行动建议伴行 (action suggestions attached)
- 绝对不生成焦虑信号 (never anxiety-inducing)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MoodTrend(StrEnum):
    """Overall emotional trend for the week."""

    IMPROVING = "improving"        # better than last week
    STABLE = "stable"              # similar to last week
    NEEDS_ATTENTION = "needs_attention"  # more negative dips than last week


@dataclass
class WeeklyTrend:
    """Day-by-day valence trend for the week."""

    start_date: str
    end_date: str
    daily_valences: dict[str, float] = field(default_factory=dict)
    overall_trend: MoodTrend = MoodTrend.STABLE
    positive_days: int = 0
    negative_dips: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "daily_valences": self.daily_valences,
            "overall_trend": self.overall_trend.value,
            "positive_days": self.positive_days,
            "negative_dips": self.negative_dips,
        }


@dataclass
class EmotionWeeklyReport:
    """A complete parent-facing weekly emotion report.

    Built from the child's emotion baseline + safety/interest events
    over the past 7 days.
    """

    child_id: str
    trend: WeeklyTrend
    headline: str                     # one-sentence positive summary
    discoveries: list[str]             # positive findings (exploration, growth)
    fuzzy_negatives: list[str]         # action-language negatives
    anomaly_annotations: list[str]     # abnormal-baseline flags
    action_suggestions: list[str]      # actionable advice
    safety_valve_count: int = 0        # tutoring safety-valve events
    treehole_engagement_count: int = 0  # tree-hole conversations (anonymized)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    week_start: str = ""
    week_end: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "headline": self.headline,
            "discoveries": self.discoveries,
            "fuzzy_negatives": self.fuzzy_negatives,
            "anomaly_annotations": self.anomaly_annotations,
            "action_suggestions": self.action_suggestions,
            "safety_valve_count": self.safety_valve_count,
            "treehole_engagement_count": self.treehole_engagement_count,
            "trend": self.trend.to_dict(),
            "week_start": self.week_start,
            "week_end": self.week_end,
            "generated_at": self.generated_at.isoformat(),
        }
