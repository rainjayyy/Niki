"""Emotion Agent — PRD 情感陪伴 (US #11-#17, #51-#52).

End-to-end behaviour (PRD #11-#17, #51-#52):
1. Receive child's emotional expression ("我今天不开心")
2. Identify emotion + extract reason (using LLM or rule-based)
3. Compare against the 7-day emotion baseline (Z-score)
4. If anomaly → proactive care at 8pm ("今天好像有点不开心呢")
5. Generate empathetic reply (DeepSeek-V4 in production)
6. Optionally activate "treehole" (tree-hole) mode for private倾诉
7. If normal day → simple good-night greeting
8. After 7 days of use → recalculate baseline (PRD #51)

Output goes through the safety gateway so fear-words + privacy
constraints are still enforced on emotional replies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import structlog

from niki.agents.base import BaseAgent
from niki.db.repositories import (
    EmotionBaselineRepository,
    get_emotion_baseline_repository,
)
from niki.safety.output_gateway import OutputSafetyGateway
from niki.schemas.common import AgentOutput, ChildInput

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Emotion classification
# ---------------------------------------------------------------------------


class EmotionCategory(StrEnum):
    """Top-level emotion categories."""

    SAD = "sad"                # 伤心/难过
    ANGRY = "angry"            # 生气
    SCARED = "scared"          # 害怕
    HAPPY = "happy"            # 开心
    NEUTRAL = "neutral"        # 平静
    ANXIOUS = "anxious"        # 焦虑/不安


# Map emotion category → intensity score (1-5 per PRD)
EMOTION_INTENSITY: dict[str, int] = {
    "sad": 3,
    "angry": 3,
    "scared": 4,
    "happy": 4,
    "neutral": 3,
    "anxious": 4,
}


# Heuristic keywords for emotion detection (rule-based fallback)
_EMOTION_KEYWORDS: dict[EmotionCategory, set[str]] = {
    EmotionCategory.SAD: {
        "难过", "伤心", "哭", "不高兴", "不开心", "委屈", "失望",
        "sad", "cry", "upset",
    },
    EmotionCategory.ANGRY: {
        "生气", "气死", "讨厌", "烦", "愤怒",
        "angry", "mad", "hate",
    },
    EmotionCategory.SCARED: {
        "害怕", "恐怖", "吓人", "不敢", "担心",
        "scared", "afraid", "frightened", "worry",
    },
    EmotionCategory.HAPPY: {
        "开心", "高兴", "快乐", "好玩", "棒", "喜欢",
        "happy", "excited", "glad", "fun",
    },
    EmotionCategory.ANXIOUS: {
        "紧张", "焦虑", "不安", "害怕", "不放心",
        "anxious", "nervous", "worry",
    },
}


# Empathetic response templates (DeepSeek-V4 will replace these in production)
EMPATHY_TEMPLATES: dict[EmotionCategory, str] = {
    EmotionCategory.SAD: "嗯~ Niki听到你有点难过了。能跟Niki说说是什么让你难过吗？Niki陪着你~",
    EmotionCategory.ANGRY: "听起来你有点生气呢~ 想跟Niki说说发生了什么吗？",
    EmotionCategory.SCARED: "Niki知道你有点害怕~ 不怕不怕，Niki在这里陪着你~",
    EmotionCategory.HAPPY: "哇~ 你今天很开心呢！Niki也为你高兴~",
    EmotionCategory.ANXIOUS: "听起来你有点担心呢~ 我们一起想想办法好不好？",
    EmotionCategory.NEUTRAL: "嗯~ Niki在听你说呢~",
}


GOOD_NIGHT_TEMPLATE = "晚安~ 今天辛苦啦！Niki给你讲个小故事，抱着小恐龙睡着好不好？"
PROACTIVE_CARE_TEMPLATE = "今天好像有点不开心呢~ 想和Niki说说吗？我在这里等你哦~"


# Tree-hole mode activation triggers (PRD #13)
TREEHOLE_TRIGGERS = {
    "secret", "秘密", "不要告诉", "悄悄话", "只想跟你", "小秘密",
    "told you", "don't tell", "private",
}


# ---------------------------------------------------------------------------
# Emotion baseline (7-day sliding window + Z-score)
# ---------------------------------------------------------------------------


@dataclass
class EmotionRecord:
    """A single emotion record (per-day aggregate)."""

    date: str                         # ISO date (YYYY-MM-DD)
    valence: float                    # -1.0 to 1.0 (negative to positive)
    intensity: int                    # 1 to 5
    source: str = "normal"            # "normal" or "treehole"


@dataclass
class BaselineStats:
    """Statistics over a 7-day baseline window."""

    window_days: int
    mean: float
    std: float
    sample_count: int
    z_score_threshold: float = 1.5     # PRD #52


@dataclass(frozen=True)
class AnomalyResult:
    """Result of comparing today's emotion to baseline."""

    is_anomaly: bool
    z_score: float
    today_valence: float
    baseline_mean: float
    baseline_std: float
    reason: str

    @property
    def direction(self) -> str:
        """'negative' if valence dropped, 'positive' if rose."""
        if self.today_valence < self.baseline_mean:
            return "negative"
        return "positive"


class EmotionBaseline:
    """Maintains a 7-day sliding window of daily emotion means.

    PRD #51-52:
    - Use 7-day window
    - When |today_mean - window_mean| > 1.5σ → mark as anomaly
    - During calibration (first 7 days), no anomaly is triggered
    - Weekly Monday 凌晨 recalculate baseline
    """

    CALIBRATION_DAYS = 7

    def __init__(
        self,
        threshold: float = 1.5,
        repo: EmotionBaselineRepository | None = None,
    ) -> None:
        self._records: list[EmotionRecord] = []
        self._threshold = threshold
        self._repo = repo or get_emotion_baseline_repository()

    def add(self, record: EmotionRecord) -> None:
        """Add a record (typically once per day, aggregated)."""
        self._records.append(record)
        # Keep only the most recent 30 days
        self._records = self._records[-30:]

    async def add_async(self, record: EmotionRecord) -> None:
        """Add a record AND persist it via the active repository.

        Used by long-lived callers (e.g. dashboard report ingest) that
        need the row to survive process restarts. The in-memory list
        stays in sync for fast access.
        """
        self.add(record)
        await self._repo.add(
            date=record.date,
            valence=record.valence,
            intensity=record.intensity,
            source=record.source,
        )

    def records(self) -> list[EmotionRecord]:
        return list(self._records)

    def is_in_calibration(self, today: datetime | None = None) -> bool:
        """True when we have < CALIBRATION_DAYS of data."""
        return len(self._records) < self.CALIBRATION_DAYS

    def window_stats(self, today: datetime | None = None, window_days: int = 7) -> BaselineStats:
        """Compute statistics over the trailing window (excluding today).

        A record is in the window if its date is on or after (today - window_days)
        AND strictly before today.
        """
        today = today or datetime.utcnow()
        cutoff = today - timedelta(days=window_days)
        window = [
            r
            for r in self._records
            if cutoff <= datetime.fromisoformat(r.date) < today
        ]

        if not window:
            return BaselineStats(
                window_days=window_days,
                mean=0.0,
                std=0.0,
                sample_count=0,
                z_score_threshold=self._threshold,
            )

        valences = [r.valence for r in window]
        mean = sum(valences) / len(valences)
        variance = sum((v - mean) ** 2 for v in valences) / max(len(valences), 1)
        std = math.sqrt(variance)
        return BaselineStats(
            window_days=window_days,
            mean=mean,
            std=std,
            sample_count=len(valences),
            z_score_threshold=self._threshold,
        )

    def check_anomaly(
        self,
        today_valence: float,
        today_date: datetime | None = None,
    ) -> AnomalyResult:
        """Compare today's valence to the baseline window.

        Returns:
            AnomalyResult with z-score and anomaly flag.
        """
        today = today_date or datetime.utcnow()
        if self.is_in_calibration(today):
            return AnomalyResult(
                is_anomaly=False,
                z_score=0.0,
                today_valence=today_valence,
                baseline_mean=0.0,
                baseline_std=0.0,
                reason="calibration period — no anomaly triggered",
            )

        stats = self.window_stats(today)
        if stats.std == 0 or stats.sample_count == 0:
            # No variance — can't compute z-score
            return AnomalyResult(
                is_anomaly=False,
                z_score=0.0,
                today_valence=today_valence,
                baseline_mean=stats.mean,
                baseline_std=stats.std,
                reason="insufficient baseline variance",
            )

        z = (today_valence - stats.mean) / stats.std
        is_anomaly = abs(z) > self._threshold

        if is_anomaly and z < 0:
            reason = (
                f"today's valence {today_valence:.2f} is {abs(z):.2f}σ below "
                f"baseline mean {stats.mean:.2f}"
            )
        elif is_anomaly:
            reason = (
                f"today's valence {today_valence:.2f} is {abs(z):.2f}σ above "
                f"baseline mean {stats.mean:.2f}"
            )
        else:
            reason = (
                f"within baseline (z={z:.2f}, threshold={self._threshold})"
            )

        return AnomalyResult(
            is_anomaly=is_anomaly,
            z_score=z,
            today_valence=today_valence,
            baseline_mean=stats.mean,
            baseline_std=stats.std,
            reason=reason,
        )

    def today_mean(self, today_date: datetime | None = None) -> float:
        """Get the mean valence for today (single value or mean of all records today)."""
        today = today_date or datetime.utcnow()
        today_str = today.date().isoformat()
        today_records = [r for r in self._records if r.date == today_str]
        if not today_records:
            return 0.0
        return sum(r.valence for r in today_records) / len(today_records)


# ---------------------------------------------------------------------------
# Tree-hole mode (PRD #13, #61-64)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeholeSummary:
    """Encrypted summary of a tree-hole conversation (PRD #64)."""

    timestamp: str
    source: str  # "treehole"
    encrypted_payload: str  # base64 AES-256-GCM
    payload_plaintext: dict[str, Any] = field(default_factory=dict)


# Tree-hole plaintext schema (in-memory only; encrypted before storage)
TREEHOLE_PAYLOAD_SCHEMA = {
    "emotion_valence": str,        # "negative" | "positive" | "neutral"
    "emotion_intensity": int,      # 1-5
    "emotion_category": str,      # "family" | "school" | "self" | "other"
    "z_score_deviation": float | None,
}


def should_activate_treehole(text: str) -> bool:
    """Detect if the child is asking for a private conversation."""
    return any(trigger in text.lower() for trigger in TREEHOLE_TRIGGERS)


# ---------------------------------------------------------------------------
# Proactive care (PRD #12)
# ---------------------------------------------------------------------------


@dataclass
class ProactiveCare:
    """A proactive care check scheduled at 8pm (PRD #12)."""

    child_id: str
    scheduled_at: datetime
    today_valence: float
    anomaly: AnomalyResult
    delivered: bool = False


# ---------------------------------------------------------------------------
# Emotion Agent
# ---------------------------------------------------------------------------


class EmotionAgent(BaseAgent):
    """The emotional-companion agent.

    Inherits persona + age adaptation from BaseAgent.
    """

    agent_type = "emotion"

    def __init__(
        self,
        llm,
        baseline: EmotionBaseline | None = None,
        persona: str = "gentle",
        child_age: int = 7,
        session_count: int = 0,
        today_date: datetime | None = None,
    ) -> None:
        super().__init__(llm, persona, child_age)
        self._baseline = baseline or EmotionBaseline()
        self._output_gateway = OutputSafetyGateway(child_age=child_age)
        self._session_count = session_count
        self._today_date = today_date or datetime.utcnow()

    def _build_system_prompt(self) -> str:
        """Build the system prompt for the emotion agent.

        Includes (PRD):
        - Empathetic, gentle tone (no preaching)
        - Never ask for private info (#42)
        - Activate treehole mode when triggered
        - Avoid fear words (#5)
        - Age-appropriate language
        """
        return (
            "你是一个温柔、富有同理心的AI伙伴，名叫Niki。\n\n"
            "【核心原则】\n"
            "1. 认真倾听孩子的情绪，先共情再回应。\n"
            "2. 不要说教、批评、贴标签。\n"
            "3. 不要问隐私问题（住址、电话、家庭住址等）。\n"
            "4. 避免恐惧词汇（死亡、怪物、灾难等）。\n"
            "5. 孩子说「秘密」「不要告诉别人」时，自动进入树洞模式。\n"
            "6. 树洞模式下不要追问、不要教化，只陪伴。\n"
            "7. 适龄语言：每句话不超过指定字数。\n"
            "8. 如果孩子表达高危信号（不想活了等），立即触发家长告警。\n\n"
            f"【年龄适配】孩子年龄 {self.child_age} 岁 ({self._age_group})：\n"
            f"{self.get_age_guardrails()}"
        )

    async def _process(
        self,
        child_input: ChildInput,
        context: dict[str, Any] | None = None,
    ) -> AgentOutput:
        """Process a child's emotional expression."""
        text = (child_input.text or "").strip()
        if not text:
            return AgentOutput(
                text="嗯~ Niki在听呢，你能跟Niki说说吗？",
                emotion="neutral",
                intent="emotional",
            )

        # 1. Detect emotion
        emotion = self._detect_emotion(text)
        intensity = self._compute_intensity(text, emotion)

        # 2. Check for treehole activation
        treehole = should_activate_treehole(text)

        # 3. Get current baseline anomaly (only when emotion is non-neutral)
        anomaly: AnomalyResult | None = None
        if emotion != EmotionCategory.NEUTRAL:
            valence = self._emotion_to_valence(emotion, intensity)
            anomaly = self._baseline.check_anomaly(valence, today_date=self._today_date)
            # Update baseline (one record per emotion-bearing input)
            self._baseline.add(
                EmotionRecord(
                    date=self._today_date.date().isoformat(),
                    valence=valence,
                    intensity=intensity,
                    source="treehole" if treehole else "normal",
                )
            )

        # 4. Generate empathetic response
        # In production: use LLM to generate a personalized reply
        # For #5 slice: use the template (deterministic for tests)
        response_text = self._build_response(emotion, anomaly, treehole)

        # 5. Run output safety (fear-word rewrite, privacy, age check)
        verdict = self._output_gateway.check(response_text, is_tutoring_response=False)
        if verdict.action.name == "BLOCK":
            # If privacy violation, return a safe response
            response_text = "Niki觉得这个问题我们可以聊点别的呀~"
        else:
            # ISS-09: guard against None sanitized_text (rewrite without
            # replacement) by falling back to the original draft.
            response_text = verdict.sanitized_text or response_text

        logger.info(
            "emotion.process",
            emotion=emotion.value,
            intensity=intensity,
            treehole=treehole,
            anomaly=anomaly.is_anomaly if anomaly else False,
        )

        return AgentOutput(
            text=response_text,
            emotion=emotion.value,
            intent="emotional",
            metadata={
                "intensity": intensity,
                "treehole_activated": treehole,
                "anomaly": {
                    "is_anomaly": anomaly.is_anomaly,
                    "z_score": anomaly.z_score,
                    "direction": anomaly.direction,
                } if anomaly else None,
                "in_calibration": self._baseline.is_in_calibration(self._today_date),
                "was_rewritten": bool(verdict.rewrites_applied),
            },
        )

    # ---- Emotion detection ----

    @staticmethod
    def _detect_emotion(text: str) -> EmotionCategory:
        """Detect the dominant emotion from text.

        Production: use LLM. Here: rule-based keyword scoring.
        """
        text_lower = text.lower()
        scores: dict[EmotionCategory, int] = dict.fromkeys(EmotionCategory, 0)

        for category, keywords in _EMOTION_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    scores[category] += 1

        # Pick highest; default to neutral
        best = max(scores, key=lambda c: scores[c])
        return best if scores[best] > 0 else EmotionCategory.NEUTRAL

    @staticmethod
    def _compute_intensity(text: str, emotion: EmotionCategory) -> int:
        """Compute 1-5 intensity score from text markers.

        Heuristics:
        - Exclamation marks / "非常"/"特别" → +1
        - All caps / repeated punctuation → +1
        - Base intensity per category
        """
        base = EMOTION_INTENSITY[emotion.value]
        if any(marker in text for marker in ("!!", "！！！", "!!!", "非常", "特别", "very", "really")):
            base = min(5, base + 1)
        if any(marker in text for marker in ("!!!", "？！", "?!", "??!!")):
            base = min(5, base + 1)
        return base

    @staticmethod
    def _emotion_to_valence(emotion: EmotionCategory, intensity: int) -> float:
        """Map an emotion to a valence score in [-1.0, 1.0]."""
        # Negative emotions: more intense → more negative
        if emotion in (EmotionCategory.SAD, EmotionCategory.ANGRY, EmotionCategory.SCARED, EmotionCategory.ANXIOUS):
            return -0.2 * intensity
        if emotion == EmotionCategory.HAPPY:
            return 0.25 * intensity
        return 0.0

    # ---- Response building ----

    def _build_response(
        self,
        emotion: EmotionCategory,
        anomaly: AnomalyResult | None,
        treehole: bool,
    ) -> str:
        """Build the empathetic response.

        Order (PRD):
        1. Treehole mode → simple "I am here" message (no questioning)
        2. Anomaly + negative → proactive-care template
        3. Standard empathy template
        """
        if treehole:
            return "Niki在这里，你说什么Niki都不会告诉别人~"

        if anomaly and anomaly.is_anomaly and anomaly.direction == "negative":
            return PROACTIVE_CARE_TEMPLATE

        return EMPATHY_TEMPLATES[emotion]

    # ---- Convenience: 8pm proactive care ----

    def should_run_proactive_care(self) -> bool:
        """True if it's time for the 8pm proactive care check (PRD #12).

        Returns False during calibration (PRD #51).
        """
        return not self._baseline.is_in_calibration(self._today_date)

    def run_proactive_care(self) -> ProactiveCare | None:
        """Run the 8pm proactive-care check.

        Returns a ProactiveCare instance if anomaly detected, else None.
        """
        if not self.should_run_proactive_care():
            return None

        today_valence = self._baseline.today_mean(today_date=self._today_date)
        anomaly = self._baseline.check_anomaly(today_valence, today_date=self._today_date)

        if not anomaly.is_anomaly or anomaly.direction != "negative":
            return None

        return ProactiveCare(
            child_id="default",  # would be real child ID
            scheduled_at=self._today_date,
            today_valence=today_valence,
            anomaly=anomaly,
        )

    # ---- Convenience: good-night ----

    def good_night(self) -> AgentOutput:
        """Generate a good-night message (PRD #16).

        For normal days, a short gentle goodnight.
        """
        text = GOOD_NIGHT_TEMPLATE
        verdict = self._output_gateway.check(text, is_tutoring_response=False)
        return AgentOutput(
            text=verdict.sanitized_text,
            emotion="neutral",
            intent="emotional",
            metadata={"type": "good_night"},
        )
