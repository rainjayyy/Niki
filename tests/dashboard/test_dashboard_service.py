"""Tests for the dashboard service — weekly reports, interest, learning, alerts.

Updated for BE-STORG-001-ext-C (DashboardService now persists via
DashboardRepository instead of in-memory dict-of-lists).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.service import DashboardService
from niki.db.repositories import (
    InMemoryDashboardRepository,
    set_dashboard_repository,
)


@pytest.fixture
def service() -> DashboardService:
    repo = InMemoryDashboardRepository()
    set_dashboard_repository(repo)
    return DashboardService(repo=repo)


@pytest.fixture
def populated_baseline() -> EmotionBaseline:
    """7 emotion records all WITHIN the 7-day window of today=2026-07-22."""
    baseline = EmotionBaseline()
    today = datetime(2026, 7, 22)
    for d in range(7):
        # days: today-1, today-2, ..., today-7 — all within the 7d window
        baseline.add(
            EmotionRecord(
                date=(today - timedelta(days=d + 1)).date().isoformat(),
                valence=0.2 - 0.05 * d,
                intensity=3,
            )
        )
    return baseline


class TestWeeklyReport:
    async def test_generates_report(
        self,
        service: DashboardService,
        populated_baseline: EmotionBaseline,
    ) -> None:
        report = await service.generate_weekly_report(
            "c1", baseline=populated_baseline, today=datetime(2026, 7, 22)
        )
        assert report.child_id == "c1"
        # 7 days in the populated baseline
        assert 6 <= len(report.trend.daily_valences) <= 7

    async def test_no_data_returns_empty_report(
        self, service: DashboardService
    ) -> None:
        empty = EmotionBaseline()
        report = await service.generate_weekly_report(
            "c1", baseline=empty, today=datetime(2026, 7, 22)
        )
        assert report.trend.daily_valences == {}
        assert "数据" in report.headline or "🙂" in report.headline

    async def test_positive_trend_uses_positive_headline(
        self,
        service: DashboardService,
    ) -> None:
        baseline = EmotionBaseline()
        # Pin today so the window slides are date-stable.
        from datetime import datetime, timezone
        today = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for d in range(3):
            baseline.add(
                EmotionRecord(
                    date=f"2026-07-{(today.day - 1) - d:02d}",
                    valence=0.5,
                    intensity=4,
                )
            )
        report = await service.generate_weekly_report(
            "c1", baseline=baseline, today=today
        )
        assert "🌞" in report.headline or "积极" in report.headline

    async def test_negative_trend_uses_action_headline(
        self,
        service: DashboardService,
    ) -> None:
        baseline = EmotionBaseline()
        from datetime import datetime, timezone
        today = datetime(2026, 7, 30, tzinfo=timezone.utc)
        for d in range(3):
            baseline.add(
                EmotionRecord(
                    date=f"2026-07-{(today.day - 1) - d:02d}",
                    valence=-0.5,
                    intensity=2,
                )
            )
        report = await service.generate_weekly_report(
            "c1", baseline=baseline, today=today
        )
        assert "🌧" in report.headline or "建议" in report.headline


class TestInterestGraph:
    async def test_no_data_returns_empty(
        self, service: DashboardService
    ) -> None:
        graph = await service.generate_interest_graph("c1")
        assert graph.top_topics == []
        assert graph.new_interests == []

    async def test_records_and_aggregates(
        self, service: DashboardService
    ) -> None:
        for i in range(3):
            await service.record_knowledge_query(
                child_id="c1", parent_id="p1",
                question=f"恐龙为什么消失 {i}",
                topic="dinosaurs",
            )
        graph = await service.generate_interest_graph("c1")
        assert any(t.name == "dinosaurs" for t in graph.top_topics)

    async def test_isolates_per_child(
        self, service: DashboardService
    ) -> None:
        await service.record_knowledge_query(
            child_id="c1", parent_id="p1",
            question="astronomy q", topic="astronomy",
        )
        await service.record_knowledge_query(
            child_id="c2", parent_id="p1",
            question="biology q", topic="biology",
        )
        g1 = await service.generate_interest_graph("c1")
        g2 = await service.generate_interest_graph("c2")
        assert any(t.name == "astronomy" for t in g1.top_topics)
        assert any(t.name == "biology" for t in g2.top_topics)
        assert not any(t.name == "biology" for t in g1.top_topics)
        assert not any(t.name == "astronomy" for t in g2.top_topics)


class TestLearningWhitepaper:
    async def test_no_data_returns_empty(
        self, service: DashboardService
    ) -> None:
        paper = await service.generate_learning_whitepaper("c1")
        assert paper.new_knowledge_explored == []
        assert paper.total_questions_asked == 0

    async def test_hard_problems_includes_safety_valve(
        self, service: DashboardService
    ) -> None:
        await service.record_safety_valve(
            child_id="c1", parent_id="p1",
            problem_summary="fraction comparison",
            round_reached=4,
        )
        paper = await service.generate_learning_whitepaper("c1")
        assert any(
            "fraction" in p for p in paper.hard_problems_attempted
        )

    async def test_action_suggestions_present(
        self, service: DashboardService
    ) -> None:
        await service.record_safety_valve(
            child_id="c1", parent_id="p1",
            problem_summary="x",
        )
        paper = await service.generate_learning_whitepaper("c1")
        assert len(paper.action_suggestions) > 0

    async def test_counts_questions_and_solved(
        self, service: DashboardService
    ) -> None:
        await service.record_knowledge_query(
            child_id="c1", parent_id="p1",
            question="q1", topic="t1",
        )
        await service.record_knowledge_query(
            child_id="c1", parent_id="p1",
            question="q2", topic="t2",
        )
        await service.record_tutoring_event(
            child_id="c1", parent_id="p1",
            problem="p1", solved=True,
        )
        paper = await service.generate_learning_whitepaper("c1")
        assert paper.total_questions_asked == 2
        assert paper.total_problems_solved == 1


class TestAlerts:
    async def test_record_safety_valve(self, service: DashboardService) -> None:
        row_id = await service.record_safety_valve(
            child_id="c1", parent_id="p1",
            problem_summary="fraction comparison",
        )
        assert isinstance(row_id, str) and len(row_id) > 0
        # Listed under safety_valve
        pending = await service.get_pending_alerts("c1")
        assert len(pending["safety_valve"]) == 1
        assert len(pending["high_risk"]) == 0

    async def test_record_high_risk(self, service: DashboardService) -> None:
        row_id = await service.record_high_risk(
            child_id="c1", parent_id="p1",
            original_text="I want to hurt myself",
        )
        assert isinstance(row_id, str)
        pending = await service.get_pending_alerts("c1")
        assert len(pending["high_risk"]) == 1
        assert len(pending["safety_valve"]) == 0

    async def test_pending_alerts_filters_by_child(
        self, service: DashboardService
    ) -> None:
        await service.record_safety_valve(
            child_id="c1", parent_id="p1", problem_summary="x"
        )
        await service.record_safety_valve(
            child_id="c2", parent_id="p1", problem_summary="y"
        )
        p1 = await service.get_pending_alerts("c1")
        p2 = await service.get_pending_alerts("c2")
        assert len(p1["safety_valve"]) == 1
        assert len(p2["safety_valve"]) == 1

    async def test_mark_alert_delivered(
        self, service: DashboardService
    ) -> None:
        row_id = await service.record_high_risk(
            child_id="c1", parent_id="p1", original_text="x"
        )
        ok = await service.mark_alert_delivered(row_id)
        assert ok is True
        # After delivered, not in pending
        pending = await service.get_pending_alerts("c1")
        assert len(pending["high_risk"]) == 0

    async def test_mark_unknown_alert_fails(
        self, service: DashboardService
    ) -> None:
        ok = await service.mark_alert_delivered("nonexistent-id")
        assert ok is False


class TestRepoIndependence:
    """Make sure two services backed by the same repo share state (for
    cross-instance deployment)."""

    async def test_two_services_share_data(self) -> None:
        repo = InMemoryDashboardRepository()
        set_dashboard_repository(repo)
        s1 = DashboardService(repo=repo)
        s2 = DashboardService(repo=repo)
        await s1.record_knowledge_query(
            child_id="c1", parent_id="p1",
            question="shared", topic="t",
        )
        graph = await s2.generate_interest_graph("c1")
        assert any(t.name == "t" for t in graph.top_topics)
