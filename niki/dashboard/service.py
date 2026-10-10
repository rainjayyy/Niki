"""Dashboard service — orchestrates report generation (BE-STORG-001-ext-C).

Reads from the various data sources (emotion baseline, knowledge base,
tutoring event log) and assembles the parent-facing reports.

The runtime state (alerts, events, queries) is now persisted via
`DashboardRepository` (BE-STORG-001-ext-C). The default backend
is in-memory; production swaps in `PostgresDashboardRepository`.

BE-STORG-001-ext-D: the weekly report now surfaces the anonymised
treehole aggregate (count + mean valence) by reading from
``TreeholeManager.aggregate_for_baseline()`` (O(1) per child) instead
of decrypting every stored engagement. Original text is never
exposed — the aggregate is the only thing that crosses the
treehole boundary, in line with PRD #64 "聚合即匿名".
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from niki.agents.emotion import EmotionBaseline
from niki.dashboard.interest import InterestGraph, InterestTopic
from niki.dashboard.learning import LearningWhitepaper, NewVocabulary
from niki.dashboard.report import (
    EmotionWeeklyReport,
    WeeklyTrend,
)
from niki.db.repositories import (
    DashboardRepository,
    get_dashboard_repository,
)
from niki.treehole.manager import TreeholeManager

logger = structlog.get_logger(__name__)


# Back-compat dataclasses preserved so any caller (or older tests) can
# still construct alert objects. The new repo path doesn't require
# them — records are stored as plain dicts in dashboard_alerts.payload.
@dataclass(frozen=True)
class SafetyValveAlert:
    id: str
    child_id: str
    problem_summary: str
    round_reached: int = 4
    severity: str = "info"
    action_suggestion: str = ""
    delivered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HighRiskAlert:
    id: str
    child_id: str
    original_text: str
    niki_response_summary: str = ""
    severity: str = "urgent"
    delivered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertSeverity:
    INFO = "info"
    URGENT = "urgent"


class DashboardService:
    """Generates the parent-facing dashboard reports.

    Pulls from:
    - EmotionBaseline (#5) for weekly trend
    - KnowledgeBase (#3) for interest graph
    - Tutoring events log (#4) for safety-valve alerts + hard problems
    - Safety pipeline (#1) for high-risk alerts

    Persistence is via `DashboardRepository` (BE-STORG-001-ext-C).
    """

    def __init__(
        self,
        repo: DashboardRepository | None = None,
        treehole_manager: TreeholeManager | None = None,
    ) -> None:
        self._repo = repo or get_dashboard_repository()
        # BE-STORG-001-ext-D: optional TreeholeManager dependency for
        # reading the per-child O(1) rolling aggregate. When ``None``,
        # the dashboard instantiates a default manager — that path
        # is what the API and tests use.
        self._treehole_manager = treehole_manager or TreeholeManager()

    @property
    def repo(self) -> DashboardRepository:
        return self._repo

    # ---- Alerts ingestion ----

    async def record_safety_valve(
        self,
        child_id: str,
        parent_id: str,
        problem_summary: str,
        round_reached: int = 4,
    ) -> str:
        """Record a tutoring safety-valve trigger (PRD #27)."""
        alert_id = self._make_id(child_id, problem_summary, round_reached)
        payload = {
            "id": alert_id,
            "problem_summary": problem_summary,
            "round_reached": round_reached,
            "severity": AlertSeverity.INFO,
            "action_suggestion": (
                "这周孩子在这类题上需要一些帮助。"
                "建议周末找时间和孩子一起看看这类问题，"
                "用更生活化的例子帮助理解。"
            ),
        }
        row_id = await self._repo.add_alert(
            alert_type="safety_valve",
            child_id=child_id,
            parent_id=parent_id,
            payload=payload,
        )
        logger.info(
            "dashboard.safety_valve.recorded",
            row_id=row_id,
            child_id=child_id,
            problem=problem_summary,
        )
        return row_id

    async def record_high_risk(
        self,
        child_id: str,
        parent_id: str,
        original_text: str,
        niki_response_summary: str = "Niki 已用温柔的话语回应孩子",
    ) -> str:
        """Record a high-risk signal (PRD #29)."""
        alert_id = self._make_id(child_id, original_text, 0)
        payload = {
            "id": alert_id,
            "original_text": original_text,
            "niki_response_summary": niki_response_summary,
            "severity": AlertSeverity.URGENT,
        }
        row_id = await self._repo.add_alert(
            alert_type="high_risk",
            child_id=child_id,
            parent_id=parent_id,
            payload=payload,
        )
        logger.warning(
            "dashboard.high_risk.recorded",
            row_id=row_id,
            child_id=child_id,
        )
        return row_id

    async def record_tutoring_event(
        self,
        child_id: str,
        parent_id: str,
        problem: str,
        solved: bool = False,
        safety_valve: bool = False,
    ) -> str:
        """Record a tutoring event for the whitepaper."""
        payload = {
            "problem": problem,
            "solved": solved,
            "safety_valve": safety_valve,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._repo.add_tutoring_event(
            child_id=child_id,
            parent_id=parent_id,
            payload=payload,
        )

    async def record_knowledge_query(
        self,
        child_id: str,
        parent_id: str,
        question: str,
        topic: str = "",
    ) -> str:
        """Record a knowledge-base query for the whitepaper."""
        payload = {
            "question": question,
            "topic": topic or question,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return await self._repo.add_knowledge_query(
            child_id=child_id,
            parent_id=parent_id,
            payload=payload,
        )

    # ---- Report generation ----

    async def generate_weekly_report(
        self,
        child_id: str,
        baseline: EmotionBaseline,
        today: datetime | None = None,
    ) -> EmotionWeeklyReport:
        """Generate a weekly emotion report (PRD #24, #27)."""
        today = today or datetime.now(timezone.utc)
        week_start = today - timedelta(days=7)

        trend = self._build_trend(child_id, baseline, week_start, today)
        headline = self._compose_headline(trend)
        discoveries = self._discoveries(trend, baseline)
        fuzzy_negatives = self._fuzzy_negatives(trend)
        anomalies = self._anomaly_annotations(baseline, today)
        actions = self._action_suggestions(trend, anomalies)

        alerts = await self._repo.list_alerts(alert_type="safety_valve", child_id=child_id)
        safety_count = sum(1 for a in alerts if a.get("payload", {}).get("round_reached", 0) > 0)

        # BE-STORG-001-ext-D: read the per-child treehole rolling
        # aggregate. The manager handles "no engagements" gracefully
        # (returns count=0). Exceptions are caught so a treehole-store
        # outage can never break the parent dashboard.
        try:
            treehole_agg = await self._treehole_manager.aggregate_for_baseline(
                child_id=child_id, window_kind="weekly"
            )
            treehole_count = int(treehole_agg.get("count", 0) or 0)
            treehole_mean = float(treehole_agg.get("mean_valence", 0.0) or 0.0)
        except Exception:
            logger.exception(
                "dashboard.treehole_aggregate_failed",
                child_id=child_id,
            )
            treehole_count = 0
            treehole_mean = 0.0

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
            treehole_mean_valence=treehole_mean,
            week_start=week_start.date().isoformat(),
            week_end=today.date().isoformat(),
        )

    async def generate_interest_graph(
        self,
        child_id: str,
    ) -> InterestGraph:
        """Generate the interest graph (PRD #25)."""
        queries = await self._repo.list_knowledge_queries(child_id)

        topic_counts: dict[str, int] = {}
        topic_examples: dict[str, list[str]] = {}
        topic_dates: dict[str, list[str]] = {}

        for q in queries:
            p = q.get("payload", {})
            topic = p.get("topic", "")
            if not topic:
                continue
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
            topic_examples.setdefault(topic, []).append(p.get("question", ""))
            topic_dates.setdefault(topic, []).append(p.get("timestamp", ""))

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

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
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
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def generate_learning_whitepaper(
        self,
        child_id: str,
    ) -> LearningWhitepaper:
        """Generate the learning whitepaper (PRD #26)."""
        queries = await self._repo.list_knowledge_queries(child_id)
        alerts = await self._repo.list_alerts(alert_type="safety_valve", child_id=child_id)
        events = await self._repo.list_tutoring_events(child_id)

        new_knowledge = list(
            dict.fromkeys(
                q.get("payload", {}).get("topic", "")
                for q in queries
                if q.get("payload", {}).get("topic")
            )
        )

        hard_problems = [
            a.get("payload", {}).get("problem_summary", "")
            for a in alerts
            if a.get("payload", {}).get("problem_summary")
        ]

        new_vocab: list[NewVocabulary] = []
        for q in queries[:10]:
            p = q.get("payload", {})
            question = p.get("question", "")
            for word in question.split():
                if len(word) >= 3 and word not in [v.word for v in new_vocab]:
                    new_vocab.append(
                        NewVocabulary(
                            word=word,
                            context=question,
                            first_seen=p.get("timestamp", "").split("T")[0],
                        )
                    )

        suggestions: list[str] = []
        if hard_problems:
            suggestions.append(
                "💡 孩子在某些题目上需要更多帮助。建议周末找时间和孩子一起看看这些题目。"
            )
        if new_knowledge:
            top = new_knowledge[0]
            suggestions.append(
                f"🌟 本周探索了「{top}」相关知识。推荐周末带 TA 去科技馆/图书馆深入了解。"
            )

        total_solved = sum(1 for e in events if e.get("payload", {}).get("solved"))

        return LearningWhitepaper(
            child_id=child_id,
            new_knowledge_explored=new_knowledge[:5],
            hard_problems_attempted=hard_problems,
            new_vocabulary=new_vocab[:5],
            action_suggestions=suggestions,
            total_questions_asked=len(queries),
            total_problems_solved=total_solved,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_pending_alerts(
        self,
        child_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get all pending alerts for a child."""
        all_high_risk = await self._repo.list_alerts(alert_type="high_risk", child_id=child_id)
        all_safety = await self._repo.list_alerts(alert_type="safety_valve", child_id=child_id)
        return {
            "high_risk": [r for r in all_high_risk if r.get("delivered_at") is None],
            "safety_valve": list(all_safety),
        }

    async def mark_alert_delivered(self, alert_id: str) -> bool:
        """Mark an alert as delivered."""
        return await self._repo.mark_alert_delivered(alert_id)

    async def dismiss_alerts_batch(self, alert_ids: list[str]) -> dict[str, Any]:
        """Dismiss many alerts in one logical call (BE-API-005).

        Each id is independent: a missing id is skipped with a
        `reason` of `not_found`. The cap (100 ids per request) is
        enforced by the API layer.

        Returns:
            {
                "dismissed": <int>,
                "skipped": [{"alert_id": "...", "reason": "..."}, ...],
            }
        """
        dismissed = 0
        skipped: list[dict[str, Any]] = []
        for alert_id in alert_ids:
            if not isinstance(alert_id, str) or not alert_id:
                skipped.append({"alert_id": alert_id, "reason": "invalid"})
                continue
            ok = await self._repo.mark_alert_delivered(alert_id)
            if ok:
                dismissed += 1
            else:
                skipped.append({"alert_id": alert_id, "reason": "not_found"})
        return {"dismissed": dismissed, "skipped": skipped}

    # ---- Internals ----

    def _build_trend(
        self,
        child_id: str,
        baseline: EmotionBaseline,
        week_start: datetime,
        today: datetime,
    ) -> WeeklyTrend:
        # Trend building logic preserved (was sync, kept sync — doesn't
        # touch the repo, just slices the baseline in memory).
        ws_iso = week_start.date().isoformat()
        records = [r for r in baseline.records() if r.date >= ws_iso]
        print(
            f"DEBUG _build_trend: ws_iso={ws_iso} all_dates={[r.date for r in baseline.records()]} in_range={[r.date for r in records]}"
        )
        if not records:
            return WeeklyTrend(
                start_date=ws_iso,
                end_date=today.date().isoformat(),
                daily_valences={},
            )

        by_day: dict[str, list[float]] = {}
        for r in records:
            by_day.setdefault(r.date, []).append(r.valence)

        daily_valences: dict[str, float] = {
            day: sum(vals) / len(vals) for day, vals in by_day.items()
        }
        return WeeklyTrend(
            start_date=week_start.date().isoformat(),
            end_date=today.date().isoformat(),
            daily_valences=daily_valences,
        )

    def _compose_headline(self, trend: WeeklyTrend) -> str:
        if not trend.daily_valences:
            return "本周暂无足够数据，下周继续观察 😊"
        avg = sum(trend.daily_valences.values()) / len(trend.daily_valences)
        if avg > 0.3:
            return "🌞 本周孩子情绪整体积极向上，互动质量很高！"
        elif avg > 0:
            return "🙂 本周孩子情绪平稳，有几次温暖的对话时刻。"
        elif avg > -0.3:
            return "🌥 本周孩子有些小低落，但整体可控。"
        else:
            return "🌧 本周孩子情绪偏负面，建议找时间聊一聊。"

    def _discoveries(self, trend: WeeklyTrend, baseline: EmotionBaseline) -> list[str]:
        out: list[str] = []
        for day, val in trend.daily_valences.items():
            if val > 0.4:
                out.append(f"📈 {day} 的对话氛围特别好（平均情绪 {val:.2f}）")
        return out

    def _fuzzy_negatives(self, trend: WeeklyTrend) -> list[str]:
        out: list[str] = []
        for day, val in trend.daily_valences.items():
            if val < -0.3:
                out.append(f"⚠️ {day} 孩子情绪有点低落，注意观察")
        return out

    def _anomaly_annotations(self, baseline: EmotionBaseline, today: datetime) -> list[str]:
        # Use today_mean as today's valence proxy. If there's only one
        # record we can pass it directly.
        if not baseline.records():
            return []
        latest = baseline.records()[-1]
        anomaly = baseline.check_anomaly(
            today_valence=latest.valence,
            today_date=today,
        )
        if not anomaly.is_anomaly:
            return []
        return [f"🚨 {latest.date} 情绪偏离基线 {anomaly.z_score:.1f}σ"]

    def _action_suggestions(
        self,
        trend: WeeklyTrend,
        anomalies: list[str],
    ) -> list[str]:
        out: list[str] = []
        if anomalies:
            out.append("💡 本周出现情绪异常波动，建议回顾当天对话找根因。")
        if not trend.daily_valences:
            out.append("本周数据不足，下周保持记录节奏。")
        return out

    @staticmethod
    def _make_id(child_id: str, content: str, salt: int) -> str:
        """Stable deterministic id for an alert row."""
        raw = f"{child_id}|{content}|{salt}".encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]
