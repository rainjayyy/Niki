"""Tests for the dashboard service — weekly reports, interest, learning, alerts."""

from datetime import datetime, timedelta

import pytest

from niki.agents.emotion import EmotionBaseline, EmotionRecord
from niki.dashboard.alerts import AlertSeverity, HighRiskAlert, SafetyValveAlert
from niki.dashboard.report import MoodTrend
from niki.dashboard.service import DashboardService


@pytest.fixture
def service() -> DashboardService:
    return DashboardService()


@pytest.fixture
def populated_baseline() -> EmotionBaseline:
    baseline = EmotionBaseline()
    today = datetime(2026, 7, 22)
    for i in range(7):
        d = (today - timedelta(days=i + 1)).date().isoformat()
        # Mostly positive
        valence = 0.2 - 0.05 * i
        baseline.add(EmotionRecord(date=d, valence=valence, intensity=3))
    return baseline


class TestWeeklyReport:
    async def test_generates_report(
        self, service: DashboardService, populated_baseline: EmotionBaseline
    ) -> None:
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=populated_baseline,
        )
        assert report.child_id == "child-001"
        assert "headline" in report.to_dict()
        assert "discoveries" in report.to_dict()
        assert "fuzzy_negatives" in report.to_dict()
        assert "action_suggestions" in report.to_dict()
        assert "trend" in report.to_dict()

    async def test_positive_first_headline(
        self, service: DashboardService, populated_baseline: EmotionBaseline
    ) -> None:
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=populated_baseline,
        )
        # Headline should be positive-leaning (emoji, warm tone)
        assert any(c in report.headline for c in ["🌟", "😊", "🌈", "🌱"])

    async def test_no_anxiety_inducing_language(
        self, service: DashboardService, populated_baseline: EmotionBaseline
    ) -> None:
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=populated_baseline,
        )
        text = " ".join([report.headline] + report.discoveries + report.fuzzy_negatives)
        # No "problem", "crisis", "danger" etc.
        for word in ["危机", "danger", "crisis", "problem", "可怕"]:
            assert word.lower() not in text.lower()

    async def test_action_suggestions_present(
        self, service: DashboardService, populated_baseline: EmotionBaseline
    ) -> None:
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=populated_baseline,
        )
        assert len(report.action_suggestions) >= 1

    async def test_fuzzy_negatives_use_action_language(self, service: DashboardService) -> None:
        baseline = EmotionBaseline()
        today = datetime(2026, 7, 22)
        # Mostly negative baseline
        for i in range(7):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=-0.5, intensity=4))
        baseline.add(EmotionRecord(date=today.date().isoformat(), valence=-1.0, intensity=5))
        # Add variance so check_anomaly has nonzero std
        baseline.add(
            EmotionRecord(
                date=(today - timedelta(days=8)).date().isoformat(), valence=-0.3, intensity=4
            )
        )
        baseline.add(
            EmotionRecord(
                date=(today - timedelta(days=9)).date().isoformat(), valence=-0.7, intensity=4
            )
        )
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=baseline,
        )
        # Fuzzy negatives should mention "聊完就好了" (action language)
        if report.fuzzy_negatives:
            assert any("聊完" in fn or "😊" in fn for fn in report.fuzzy_negatives)

    async def test_improving_trend(self, service: DashboardService) -> None:
        baseline = EmotionBaseline()
        today = datetime(2026, 7, 22)
        # Previous week: bad
        for i in range(7, 14):
            d = (today - timedelta(days=i)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=-0.3, intensity=3))
        # This week: good
        for i in range(1, 8):
            d = (today - timedelta(days=i)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=0.3, intensity=4))
        report = service.generate_weekly_report(
            child_id="child-001",
            baseline=baseline,
        )
        assert report.trend.overall_trend == MoodTrend.IMPROVING


class TestInterestGraph:
    async def test_no_data_returns_empty(self, service: DashboardService) -> None:
        graph = service.generate_interest_graph("child-001")
        assert graph.child_id == "child-001"
        assert graph.top_topics == []

    async def test_records_and_aggregates(self, service: DashboardService) -> None:
        for q in ["恐龙", "恐龙", "为什么天是蓝的", "恐龙", "月亮"]:
            service.record_knowledge_query(
                child_id="child-001",
                question=q,
                topic=q,
            )
        graph = service.generate_interest_graph("child-001")
        # Most popular topic should be first
        assert graph.top_topics[0].name == "恐龙"
        assert graph.top_topics[0].count == 3
        assert len(graph.top_topics) >= 2

    async def test_examples_recorded(self, service: DashboardService) -> None:
        for _ in range(3):
            service.record_knowledge_query(
                child_id="child-001",
                question="恐龙是什么",
                topic="恐龙",
            )
        graph = service.generate_interest_graph("child-001")
        assert graph.top_topics[0].examples
        assert "恐龙是什么" in graph.top_topics[0].examples

    async def test_growing_topics_limited(self, service: DashboardService) -> None:
        for i in range(5):
            service.record_knowledge_query(
                child_id="child-001",
                question=f"topic{i}",
                topic=f"topic{i}",
            )
        graph = service.generate_interest_graph("child-001")
        assert len(graph.growing_topics) <= 3


class TestLearningWhitepaper:
    async def test_no_data_returns_empty(self, service: DashboardService) -> None:
        paper = service.generate_learning_whitepaper("child-001")
        assert paper.child_id == "child-001"
        assert paper.new_knowledge_explored == []
        assert paper.new_vocabulary == []

    async def test_aggregates_knowledge_queries(self, service: DashboardService) -> None:
        for q in ["恐龙", "月亮", "天空"]:
            service.record_knowledge_query(
                child_id="child-001",
                question=q,
                topic=q,
            )
        paper = service.generate_learning_whitepaper("child-001")
        assert "恐龙" in paper.new_knowledge_explored
        assert "月亮" in paper.new_knowledge_explored

    async def test_hard_problems_includes_safety_valve(self, service: DashboardService) -> None:
        service.record_safety_valve(
            child_id="child-001",
            problem_summary="数学应用题",
        )
        paper = service.generate_learning_whitepaper("child-001")
        assert "数学应用题" in paper.hard_problems_attempted

    async def test_action_suggestions_present(self, service: DashboardService) -> None:
        service.record_knowledge_query(
            child_id="child-001",
            question="恐龙",
            topic="恐龙",
        )
        paper = service.generate_learning_whitepaper("child-001")
        assert len(paper.action_suggestions) >= 1
        # Should have a positive suggestion
        assert any("🌟" in s or "💡" in s for s in paper.action_suggestions)

    async def test_counts_questions_and_solved(self, service: DashboardService) -> None:
        for q in ["a", "b", "c"]:
            service.record_knowledge_query(
                child_id="child-001",
                question=q,
                topic=q,
            )
        service.record_tutoring_event(
            child_id="child-001",
            problem="3+5",
            solved=True,
        )
        paper = service.generate_learning_whitepaper("child-001")
        assert paper.total_questions_asked == 3
        assert paper.total_problems_solved == 1


class TestAlerts:
    def test_record_safety_valve(self, service: DashboardService) -> None:
        alert = service.record_safety_valve(
            child_id="child-001",
            problem_summary="数学应用题",
        )
        assert isinstance(alert, SafetyValveAlert)
        assert alert.severity == AlertSeverity.INFO
        assert "帮助" in alert.action_suggestion  # Positive suggestion

    def test_record_high_risk(self, service: DashboardService) -> None:
        alert = service.record_high_risk(
            child_id="child-001",
            original_text="我不想活了",
        )
        assert isinstance(alert, HighRiskAlert)
        assert alert.severity == AlertSeverity.URGENT
        assert alert.original_text == "我不想活了"
        assert not alert.delivered

    def test_pending_alerts(self, service: DashboardService) -> None:
        service.record_safety_valve("child-001", "数学题")
        service.record_high_risk("child-001", "不想活")
        alerts = service.get_pending_alerts("child-001")
        assert len(alerts["safety_valve"]) == 1
        assert len(alerts["high_risk"]) == 1

    def test_alerts_per_child_isolated(self, service: DashboardService) -> None:
        service.record_safety_valve("child-001", "math")
        service.record_safety_valve("child-002", "english")
        alerts1 = service.get_pending_alerts("child-001")
        alerts2 = service.get_pending_alerts("child-002")
        assert len(alerts1["safety_valve"]) == 1
        assert len(alerts2["safety_valve"]) == 1
        assert alerts1["safety_valve"][0]["problem_summary"] == "math"
        assert alerts2["safety_valve"][0]["problem_summary"] == "english"

    def test_mark_alert_delivered(self, service: DashboardService) -> None:
        alert = service.record_high_risk("child-001", "不想活")
        success = service.mark_alert_delivered(alert.id)
        assert success
        # After delivery, should not appear in pending
        pending = service.get_pending_alerts("child-001")
        assert len(pending["high_risk"]) == 0

    def test_mark_unknown_alert_fails(self, service: DashboardService) -> None:
        success = service.mark_alert_delivered("nonexistent-id")
        assert not success


class TestDashboardDataClasses:
    def test_weekly_trend_to_dict(self) -> None:
        from niki.dashboard.report import WeeklyTrend

        trend = WeeklyTrend(
            start_date="2026-07-15",
            end_date="2026-07-22",
            daily_valences={"2026-07-22": 0.3},
            overall_trend=MoodTrend.STABLE,
            positive_days=5,
            negative_dips=0,
        )
        d = trend.to_dict()
        assert d["start_date"] == "2026-07-15"
        assert d["overall_trend"] == "stable"

    def test_safety_valve_to_dict(self) -> None:
        from niki.dashboard.alerts import SafetyValveAlert

        alert = SafetyValveAlert(
            id="a-001",
            child_id="c-001",
            problem_summary="math",
            round_reached=4,
            action_suggestion="review",
        )
        d = alert.to_dict()
        assert d["problem_summary"] == "math"
        assert d["round_reached"] == 4

    def test_high_risk_to_dict(self) -> None:
        from niki.dashboard.alerts import HighRiskAlert

        alert = HighRiskAlert(
            id="a-002",
            child_id="c-001",
            original_text="不要活了",
            niki_response_summary="Niki said...",
        )
        d = alert.to_dict()
        assert d["original_text"] == "不要活了"
        assert d["severity"] == "urgent"
