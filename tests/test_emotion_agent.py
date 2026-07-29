"""Tests for the Emotion Agent end-to-end."""

from datetime import datetime, timedelta, timezone

import pytest

from niki.agents.emotion import (
    EMPATHY_TEMPLATES,
    PROACTIVE_CARE_TEMPLATE,
    EmotionAgent,
    EmotionBaseline,
    EmotionCategory,
    EmotionRecord,
    should_activate_treehole,
)
from niki.schemas.common import ChildInput


class _StubLLM:
    async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return {"content": "Niki听到你了~"}


@pytest.fixture
def baseline() -> EmotionBaseline:
    return EmotionBaseline()


@pytest.fixture
def agent(baseline: EmotionBaseline) -> EmotionAgent:
    return EmotionAgent(
        llm=_StubLLM(),
        baseline=baseline,
        persona="gentle",
        child_age=7,
    )


# ---------------------------------------------------------------------------
# Emotion detection
# ---------------------------------------------------------------------------


class TestEmotionDetection:
    def test_sad(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("我今天很难过") == EmotionCategory.SAD

    def test_angry(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("我生气了") == EmotionCategory.ANGRY

    def test_scared(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("我有点害怕") == EmotionCategory.SCARED

    def test_happy(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("我今天很开心") == EmotionCategory.HAPPY

    def test_neutral_when_no_keywords(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("今天天气不错") == EmotionCategory.NEUTRAL

    def test_english_sad(self, agent: EmotionAgent) -> None:
        assert agent._detect_emotion("I am sad today") == EmotionCategory.SAD


# ---------------------------------------------------------------------------
# Intensity computation
# ---------------------------------------------------------------------------


class TestIntensity:
    def test_base_intensity(self, agent: EmotionAgent) -> None:
        assert agent._compute_intensity("我难过", EmotionCategory.SAD) == 3

    def test_exclamation_increases_intensity(self, agent: EmotionAgent) -> None:
        assert agent._compute_intensity("我非常难过！", EmotionCategory.SAD) == 4

    def test_double_punctuation_maxes(self, agent: EmotionAgent) -> None:
        assert agent._compute_intensity("我非常非常难过！？！", EmotionCategory.SCARED) == 5


# ---------------------------------------------------------------------------
# Valence mapping
# ---------------------------------------------------------------------------


class TestValence:
    def test_negative_valence(self, agent: EmotionAgent) -> None:
        v = agent._emotion_to_valence(EmotionCategory.SAD, 3)
        assert v < 0

    def test_positive_valence(self, agent: EmotionAgent) -> None:
        v = agent._emotion_to_valence(EmotionCategory.HAPPY, 4)
        assert v > 0

    def test_neutral_valence(self, agent: EmotionAgent) -> None:
        v = agent._emotion_to_valence(EmotionCategory.NEUTRAL, 3)
        assert v == 0


# ---------------------------------------------------------------------------
# EmotionBaseline — Z-score detection
# ---------------------------------------------------------------------------


class TestBaseline:
    def test_in_calibration_when_empty(self, baseline: EmotionBaseline) -> None:
        assert baseline.is_in_calibration() is True

    def test_out_of_calibration_after_7_days(self, baseline: EmotionBaseline) -> None:
        today = datetime(2026, 7, 22)
        for i in range(7):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=0.0, intensity=3))
        assert baseline.is_in_calibration(today) is False

    def test_calibration_skips_anomaly(self, baseline: EmotionBaseline) -> None:
        # In calibration: anomaly should be False even with extreme value
        result = baseline.check_anomaly(-1.0, today_date=datetime.now(timezone.utc))
        assert result.is_anomaly is False
        assert "calibration" in result.reason

    def test_no_anomaly_when_within_baseline(self, baseline: EmotionBaseline) -> None:
        today = datetime(2026, 7, 22)
        for i in range(7):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=0.0, intensity=3))
        result = baseline.check_anomaly(0.1, today_date=today)
        assert not result.is_anomaly

    def test_anomaly_when_negative_outlier(self, baseline: EmotionBaseline) -> None:
        today = datetime(2026, 7, 22)
        # 7 baseline days with mean 0 + variance
        for i, v in enumerate([0.0, 0.1, -0.1, 0.2, -0.2, 0.15, -0.05]):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=v, intensity=3))
        result = baseline.check_anomaly(-1.5, today_date=today)
        assert result.is_anomaly is True
        assert result.direction == "negative"

    def test_window_stats_empty(self, baseline: EmotionBaseline) -> None:
        stats = baseline.window_stats()
        assert stats.sample_count == 0
        assert stats.mean == 0.0

    def test_window_stats_with_data(self, baseline: EmotionBaseline) -> None:
        today = datetime(2026, 7, 22)
        # Spread records across the 7-day window
        for i, v in enumerate([0.0, 0.2, -0.1, 0.1, 0.05]):
            d = (today - timedelta(days=i + 2)).date().isoformat()
            baseline.add(EmotionRecord(date=d, valence=v, intensity=3))
        stats = baseline.window_stats(today)
        assert stats.sample_count == 5
        assert abs(stats.mean - 0.05) < 0.01

    def test_records_capped_at_30(self, baseline: EmotionBaseline) -> None:
        for i in range(50):
            baseline.add(EmotionRecord(date=f"2025-01-{i + 1:02d}", valence=0.0, intensity=3))
        assert len(baseline.records()) == 30


# ---------------------------------------------------------------------------
# Treehole mode
# ---------------------------------------------------------------------------


class TestTreeholeActivation:
    def test_activate_on_secret(self) -> None:
        assert should_activate_treehole("我有一个秘密") is True

    def test_activate_on_dont_tell(self) -> None:
        assert should_activate_treehole("不要告诉别人") is True

    def test_activate_on_english_secret(self) -> None:
        assert should_activate_treehole("I have a secret") is True

    def test_no_activation_for_normal_text(self) -> None:
        assert should_activate_treehole("我今天很开心") is False


# ---------------------------------------------------------------------------
# End-to-end agent
# ---------------------------------------------------------------------------


class TestEmotionAgentEndToEnd:
    async def test_sad_expression(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="我今天很难过", session_id="t1"))
        assert "难过" in output.text or "Niki" in output.text
        assert output.emotion == "sad"
        assert output.intent == "emotional"

    async def test_happy_expression(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="我今天好开心", session_id="t2"))
        assert output.emotion == "happy"

    async def test_scared_expression(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="我有点害怕", session_id="t3"))
        assert output.emotion == "scared"

    async def test_empty_text_prompts_to_share(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="", session_id="t4"))
        assert "说" in output.text or "听" in output.text

    async def test_treehole_activated(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="我有一个秘密", session_id="t5"))
        assert output.metadata["treehole_activated"] is True
        # Should not preach
        assert "告诉" not in output.text or "不会告诉" in output.text

    async def test_in_calibration_first_7_days(self, agent: EmotionAgent) -> None:
        # First emotion input → in calibration
        output = await agent.invoke(ChildInput(text="我难过", session_id="t6"))
        assert output.metadata["in_calibration"] is True

    async def test_metadata_includes_intensity(self, agent: EmotionAgent) -> None:
        output = await agent.invoke(ChildInput(text="我非常非常难过！！", session_id="t7"))
        assert output.metadata["intensity"] >= 4

    async def test_baseline_updated_after_expression(
        self, agent: EmotionAgent, baseline: EmotionBaseline
    ) -> None:
        await agent.invoke(ChildInput(text="我难过", session_id="t8"))
        # Should have added a record
        assert len(baseline.records()) == 1

    async def test_fear_word_rewrite_applied(self) -> None:
        """If the LLM returns fear words, the safety gateway rewrites them."""

        class LLMWithFearWord:
            async def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
                return {"content": "我看到怪物了，我也很难过"}

        baseline = EmotionBaseline()
        agent = EmotionAgent(
            llm=LLMWithFearWord(),
            baseline=baseline,
            persona="gentle",
            child_age=7,
        )
        output = await agent.invoke(ChildInput(text="我很难过", session_id="safety"))
        # Fear word "怪物" should be rewritten
        assert "怪物" not in output.text


# ---------------------------------------------------------------------------
# Proactive care
# ---------------------------------------------------------------------------


class TestProactiveCare:
    def test_in_calibration_no_care(self, agent: EmotionAgent) -> None:
        assert agent.should_run_proactive_care() is False
        assert agent.run_proactive_care() is None

    def test_no_care_when_normal(self) -> None:
        fresh_baseline = EmotionBaseline()
        today = datetime(2026, 7, 22)
        agent = EmotionAgent(
            llm=_StubLLM(),
            baseline=fresh_baseline,
            persona="gentle",
            child_age=7,
            today_date=today,
        )
        # Populate baseline with 7 normal days
        for i in range(7):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            fresh_baseline.add(EmotionRecord(date=d, valence=0.1, intensity=3))
        # Today's mean is normal
        fresh_baseline.add(EmotionRecord(date=today.date().isoformat(), valence=0.1, intensity=3))
        care = agent.run_proactive_care()
        assert care is None

    def test_care_triggered_on_anomaly(self) -> None:
        fresh_baseline = EmotionBaseline()
        today = datetime(2026, 7, 22)
        agent = EmotionAgent(
            llm=_StubLLM(),
            baseline=fresh_baseline,
            persona="gentle",
            child_age=7,
            today_date=today,
        )
        # 7 days WITH variance so z-score can compute
        # Use mixed valence values to create baseline std > 0
        for i, v in enumerate([0.1, -0.1, 0.2, -0.2, 0.15, -0.05, 0.1]):
            d = (today - timedelta(days=i + 1)).date().isoformat()
            fresh_baseline.add(EmotionRecord(date=d, valence=v, intensity=3))
        # Today: very negative (large deviation from baseline mean ~0.03)
        fresh_baseline.add(EmotionRecord(date=today.date().isoformat(), valence=-1.5, intensity=5))
        care = agent.run_proactive_care()
        assert care is not None
        assert care.anomaly.is_anomaly is True


# ---------------------------------------------------------------------------
# Good night
# ---------------------------------------------------------------------------


class TestGoodNight:
    def test_returns_greeting(self, agent: EmotionAgent) -> None:
        output = agent.good_night()
        assert "晚安" in output.text
        assert output.metadata["type"] == "good_night"

    def test_good_night_no_fear_words(self, agent: EmotionAgent) -> None:
        output = agent.good_night()
        assert "死亡" not in output.text
        assert "怪物" not in output.text


# ---------------------------------------------------------------------------
# Empathy templates
# ---------------------------------------------------------------------------


class TestEmpathyTemplates:
    def test_all_categories_have_templates(self) -> None:
        for cat in EmotionCategory:
            assert cat in EMPATHY_TEMPLATES

    def test_sad_template_empathetic(self) -> None:
        assert "难过" in EMPATHY_TEMPLATES[EmotionCategory.SAD]

    def test_proactive_care_template(self) -> None:
        assert "不开心" in PROACTIVE_CARE_TEMPLATE or "说" in PROACTIVE_CARE_TEMPLATE
