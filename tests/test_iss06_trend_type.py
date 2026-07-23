"""Tests for ISS-06: _build_trend should use list[float] for accumulation."""

from datetime import datetime, timedelta

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.service import DashboardService


class TestTrendType:
    def test_build_trend_handles_same_day_records(self) -> None:
        """ISS-06: a single date with multiple records should be averaged,
        not treated as a flat list."""
        baseline = EmotionBaseline()
        today = datetime(2026, 7, 22)
        # 3 records on the same day with different valences
        for v in [0.2, 0.4, -0.6]:
            baseline.add(
                EmotionRecord(
                    date=today.date().isoformat(),
                    valence=v,
                    intensity=3,
                )
            )

        service = DashboardService()
        trend = service._build_trend(
            child_id="c1",
            baseline=baseline,
            week_start=today - timedelta(days=2),
            week_end=today,
        )
        # One day, 3 records → average = 0.0
        assert len(trend.daily_valences) == 1
        avg = next(iter(trend.daily_valences.values()))
        assert abs(avg - 0.0) < 1e-9, f"expected ~0.0, got {avg}"
