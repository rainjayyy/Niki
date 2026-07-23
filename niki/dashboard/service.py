"""Dashboard service — orchestrates report generation.

Reads from the various data sources (emotion baseline, knowledge base,
tutoring event log) and assembles the parent-facing reports.

This is the integration point that ties together the entire Niki system
into a single weekly view for the parent.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import structlog

from niki.agents.emotion import EmotionBaseline
from niki.dashboard.alerts import (
    AlertSeverity,
    HighRiskAlert,
    SafetyValveAlert,
)
from niki.dashboard.interest import InterestGraph, InterestTopic
from niki.dashboard.learning import LearningWhitepaper, NewVocabulary
from niki.dashboard.report import (
    EmotionWeeklyReport,
    MoodTrend,
    WeeklyTrend,
)

logger = structlog.get_logger(__name__)


class DashboardService:
    """Generates the parent-facing dashboard reports.

    Pulls from:
    - EmotionBaseline (#5) for weekly trend
    - KnowledgeBase (#3) for interest graph
    - Tutoring events log (#4) for safety-valve alerts + hard problems
    - Safety pipeline (#1) for high-risk alerts
    """

    def __init__(self) -> None:
        self._safety_valve_alerts: list[SafetyValveAlert] = []
        self._high_risk_alerts: list[HighRiskAlert] = []
        # Track tutoring events for learning whitepaper
        self._tutoring_events: list[dict[str, Any]] = []
        # Track knowledge queries
        self._knowledge_queries: list[dict[str, Any]] = []

    # ---- Alerts ingestion ----

    def record_safety_valve(
        self,
        child_id: str,
        problem_summary: str,
        round_reached: int = 4,
    ) -> SafetyValveAlert:
        """Record a tutoring safety-valve trigger (PRD #27)."""
        alert = SafetyValveAlert(
            id=self._make_id(child_id, problem_summary, round_reached),
            child_id=child_id,
            severity=AlertSeverity.INFO,
            problem_summary=problem_summary,
            round_reached=round_reached,
            action_suggestion=(
                "这周孩子在这类题上需要一些帮助。"
                "建议周末找时间和孩子一起看看这类问题，"
                "用更生活化的例子帮助理解。"
            ),
        )
        self._safety_valve_alerts.append(alert)
        logger.info(
            "dashboard.safety_valve.recorded",
            child_id=child_id,
            problem=problem_summary,
        )
        return alert

    def record_high_risk(
        self,
        child_id: str,
        original_text: str,
        niki_response_summary: str = "Niki 已用温柔的话语回应孩子",
    ) -> HighRiskAlert:
        """Record a high-risk signal (PRD #29)."""
        alert = HighRiskAlert(
            id=self._make_id(child_id, original_text, 0),
            child_id=child_id,
            severity=AlertSeverity.URGENT,
            original_text=original_text,
            niki_response_summary=niki_response_summary,
        )
        self._high_risk_alerts.append(alert)
        logger.warning(
            "dashboard.high_risk.recorded",
            child_id=child_id,
        )
        return alert

    def record_tutoring_event(
        self,
        child_id: str,
        problem: str,
        solved: bool = False,
        safety_valve: bool = False,
    ) -> None:
        """Record a tutoring event for the whitepaper."""
        self._tutoring_events.append({
            "child_id": child_id,
            "problem": problem,
            "solved": solved,
            "safety_valve": safety_valve,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def record_knowledge_query(
        self,
        child_id: str,
        question: str,
        topic: str = "",
    ) -> None:
        """Record a knowledge-base query for the whitepaper."""
        self._knowledge_queries.append({
            "child_id": child_id,
            "question": question,
            "topic": topic or question,
            "timestamp": datetime.utcnow().isoformat(),
        })

    # ---- Report generation ----

    def generate_weekly_report(
        self,
        child_id: str,
        baseline: EmotionBaseline,
        today: datetime | None = None,
    ) -> EmotionWeeklyReport:
        """Generate a weekly emotion report (PRD #24, #27).

        Args:
            child_id: The child's identifier.
            baseline: The emotion baseline (5-level emotion records).
            today: Override the "today" date for testing.
        """
        today = today or datetime.utcnow()
        week_start = today - timedelta(days=7)

        # 1. Build the daily trend
        trend = self._build_trend(child_id, baseline, week_start, today)

        # 2. Compose positive-first headline
        headline = self._compose_headline(trend)

        # 3. Discoveries (positive findings)
        discoveries = self._discoveries(trend, baseline)

        # 4. Fuzzy negatives (action-language, not numbers)
        fuzzy_negatives = self._fuzzy_negatives(trend)

        # 5. Anomaly annotations
        anomalies = self._anomaly_annotations(baseline, today)

        # 6. Action suggestions
        actions = self._action_suggestions(trend, anomalies)

        # 7. Counts
        safety_count = sum(1 for a in self._safety_valve_alerts if a.child_id == child_id)
        treehole_count = 0  # Tree-hole engagement not yet tracked

        return EmotionWeeklyReport(
            child_id=child_id,
            trend=trend,
            headline=headline,
            discoveries=discoveries,
            fuzzy_negatives=fuzzy_negatives,
            anomaly_annotations=anomalies,
            action_suggestions=actions,
            safety_valve_count=safety_count,
            treehole_engagement_count=treehole_count,
            week_start=week_start.date().isoformat(),
            week_end=today.date().isoformat(),
        )

    def generate_interest_graph(
        self,
        child_id: str,
    ) -> InterestGraph:
        """Generate the interest graph (PRD #25)."""
        # Aggregate topics from knowledge queries
        topic_counts: dict[str, int] = {}
        topic_examples: dict[str, list[str]] = {}
        topic_dates: dict[str, list[str]] = {}

        for q in self._knowledge_queries:
            if q["child_id"] != child_id:
                continue
            topic = q["topic"]
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            topic_examples.setdefault(topic, []).append(q["question"])
            topic_dates.setdefault(topic, []).append(q["timestamp"])

        # Sort by count
        sorted_topics = sorted(topic_counts.items(), key=lambda x: -x[1])

        top_topics = [
            InterestTopic(
                name=name,
                count=count,
                last_seen=topic_dates[name][-1].split("T")[0] if topic_dates[name] else "",
                examples=topic_examples[name][:3],
            )
            for name, count in sorted_topics[:5]
        ]

        # "New interests" = topics seen in the last week
        week_ago = datetime.utcnow() - timedelta(days=7)
        new_interests = [
            InterestTopic(
                name=name,
                count=count,
                last_seen=topic_dates[name][-1].split("T")[0] if topic_dates[name] else "",
            )
            for name, count in sorted_topics
            if any(t >= week_ago.isoformat() for t in topic_dates.get(name, []))
        ][:3]

        return InterestGraph(
            child_id=child_id,
            top_topics=top_topics,
            new_interests=new_interests,
            growing_topics=[t.name for t in top_topics[:3]],
            generated_at=datetime.utcnow().isoformat(),
        )

    def generate_learning_whitepaper(
        self,
        child_id: str,
    ) -> LearningWhitepaper:
        """Generate the learning whitepaper (PRD #26)."""
        # New knowledge = unique encyclopedia queries
        knowledge_queries = [
            q for q in self._knowledge_queries if q["child_id"] == child_id
        ]
        new_knowledge = list(dict.fromkeys(q["topic"] for q in knowledge_queries))

        # Hard problems = safety-valve events
        hard_problems = [
            a.problem_summary
            for a in self._safety_valve_alerts
            if a.child_id == child_id
        ]

        # Vocabulary (extracted from sample queries)
        new_vocab = []
        for q in knowledge_queries[:10]:
            for word in q["question"].split():
                if len(word) >= 3 and word not in [v.word for v in new_vocab]:
                    new_vocab.append(NewVocabulary(
                        word=word,
                        context=q["question"],
                        first_seen=q["timestamp"].split("T")[0],
                    ))

        # Action suggestions
        suggestions = []
        if hard_problems:
            suggestions.append(
                "💡 孩子在某些题目上需要更多帮助。"
                "建议周末找时间和孩子一起看看这些题目。"
            )
        if new_knowledge:
            top = new_knowledge[0]
            suggestions.append(
                f"🌟 本周探索了「{top}」相关知识。"
                f"推荐周末带 TA 去科技馆/图书馆深入了解。"
            )

        # Counts
        tutoring_for_child = [
            e for e in self._tutoring_events if e["child_id"] == child_id
        ]
        total_questions = len(knowledge_queries)
        total_solved = sum(1 for e in tutoring_for_child if e.get("solved"))

        return LearningWhitepaper(
            child_id=child_id,
            new_knowledge_explored=new_knowledge[:5],
            hard_problems_attempted=hard_problems,
            new_vocabulary=new_vocab[:5],
            action_suggestions=suggestions,
            total_questions_asked=total_questions,
            total_problems_solved=total_solved,
            generated_at=datetime.utcnow().isoformat(),
        )

    def get_pending_alerts(
        self,
        child_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get all pending alerts for a child.

        Returns:
            Dict with 'high_risk' and 'safety_valve' lists.
        """
        return {
            "high_risk": [
                a.to_dict()
                for a in self._high_risk_alerts
                if a.child_id == child_id and not a.delivered
            ],
            "safety_valve": [
                a.to_dict()
                for a in self._safety_valve_alerts
                if a.child_id == child_id
            ],
        }

    def mark_alert_delivered(self, alert_id: str) -> bool:
        """Mark a high-risk alert as delivered."""
        for a in self._high_risk_alerts:
            if a.id == alert_id:
                a.delivered = True
                return True
        return False

    # ---- Internals ----

    def _build_trend(
        self,
        child_id: str,
        baseline: EmotionBaseline,
        week_start: datetime,
        week_end: datetime,
    ) -> WeeklyTrend:
        """Build the day-by-day valence trend for the week."""
        # ISS-06: was typed as `dict[str, float]` but used as a
        # list-valued accumulator. Fix the annotation.
        daily: dict[str, list[float]] = {}
        for record in baseline.records():
            record_date = datetime.fromisoformat(record.date)
            if week_start <= record_date <= week_end:
                date_str = record.date
                if date_str not in daily:
                    daily[date_str] = []
                # Accumulate per-day (multiple records per day → average)
                daily[date_str].append(record.valence)

        # Average per day
        daily_avg = {d: sum(vs) / len(vs) for d, vs in daily.items()}

        positive_days = sum(1 for v in daily_avg.values() if v > 0)
        negative_dips = sum(1 for v in daily_avg.values() if v < -0.3)

        # Compare to previous week
        prev_week_start = week_start - timedelta(days=7)
        prev_avg = []
        for record in baseline.records():
            record_date = datetime.fromisoformat(record.date)
            if prev_week_start <= record_date < week_start:
                prev_avg.append(record.valence)

        prev_mean = sum(prev_avg) / len(prev_avg) if prev_avg else 0.0
        this_mean = sum(daily_avg.values()) / len(daily_avg) if daily_avg else 0.0

        if this_mean > prev_mean + 0.05:
            trend = MoodTrend.IMPROVING
        elif this_mean < prev_mean - 0.1:
            trend = MoodTrend.NEEDS_ATTENTION
        else:
            trend = MoodTrend.STABLE

        return WeeklyTrend(
            start_date=week_start.date().isoformat(),
            end_date=week_end.date().isoformat(),
            daily_valences=daily_avg,
            overall_trend=trend,
            positive_days=positive_days,
            negative_dips=negative_dips,
        )

    def _compose_headline(self, trend: WeeklyTrend) -> str:
        """Compose a positive-first headline (never anxiety-inducing)."""
        if trend.overall_trend == MoodTrend.IMPROVING:
            return f"🌟 这周 {trend.positive_days} 天都开开心心的！"
        if trend.overall_trend == MoodTrend.NEEDS_ATTENTION:
            return "🌈 这周有几天需要一点陪伴~"
        return f"😊 这周 {trend.positive_days} 天都很平静~"

    def _discoveries(
        self,
        trend: WeeklyTrend,
        baseline: EmotionBaseline,
    ) -> list[str]:
        """Positive findings for the week."""
        discoveries = []
        if trend.positive_days >= 4:
            discoveries.append(
                f"✨ 这周有 {trend.positive_days} 天情绪特别好！"
            )
        if trend.negative_dips == 0:
            discoveries.append("🌟 这周没有明显的情绪低谷，很稳定~")
        if trend.overall_trend == MoodTrend.IMPROVING:
            discoveries.append("📈 情绪趋势相比上周明显上升！")
        return discoveries or ["🌱 这周孩子尝试了很多新事物~"]

    def _fuzzy_negatives(self, trend: WeeklyTrend) -> list[str]:
        """Convert negative dips to action-language (PRD: 模糊化负向)."""
        if trend.negative_dips == 0:
            return []
        return [
            f"有 {trend.negative_dips} 次聊完就好了😊"
        ]

    def _anomaly_annotations(
        self,
        baseline: EmotionBaseline,
        today: datetime,
    ) -> list[str]:
        """Annotate any anomalies detected by the baseline."""
        if baseline.is_in_calibration(today):
            return []
        today_valence = baseline.today_mean(today)
        result = baseline.check_anomaly(today_valence, today)
        if not result.is_anomaly:
            return []
        if result.direction == "negative":
            return [
                f"📊 今天情绪相对前一周有较明显波动（偏差 {abs(result.z_score):.1f}σ），"
                "建议今晚主动问问今天过得怎么样~"
            ]
        return []

    def _action_suggestions(
        self,
        trend: WeeklyTrend,
        anomalies: list[str],
    ) -> list[str]:
        """Generate actionable suggestions (PRD: 行动建议伴行)."""
        suggestions = []
        if trend.overall_trend == MoodTrend.NEEDS_ATTENTION:
            suggestions.append(
                "💡 建议周末安排一些孩子喜欢的活动，比如户外运动或亲子游戏~"
            )
        if trend.negative_dips > 2:
            suggestions.append(
                "💡 留意孩子最近的睡眠和饮食，情绪波动可能与作息有关~"
            )
        if not suggestions:
            suggestions.append(
                "💡 继续保持现在的陪伴节奏，孩子很享受和你分享日常~"
            )
        return suggestions

    @staticmethod
    def _make_id(child_id: str, content: str, salt: int) -> str:
        return hashlib.sha256(
            f"{child_id}:{content}:{salt}".encode()
        ).hexdigest()[:16]
