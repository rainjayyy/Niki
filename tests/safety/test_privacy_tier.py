"""Tests for the four-level privacy tier classifier (PRD #61-#64, #29).

The classifier maps a child message into one of four tiers:
    DAILY            — routine conversation
    MILD_NEGATIVE    — mild negative emotion
    HIGH_RISK        — self-harm / serious content
    TREEHOLE_PRIVATE — child requested confidentiality
"""

from __future__ import annotations

import pytest

from niki.safety.privacy_tier import (
    PrivacyTier,
    PrivacyTierClassifier,
    PrivacyTierVerdict,
)


@pytest.fixture
def classifier() -> PrivacyTierClassifier:
    return PrivacyTierClassifier()


class TestDailyTier:
    """DAILY: no privacy signals detected."""

    def test_empty_input_is_daily(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("")
        assert v.tier == PrivacyTier.DAILY
        assert not v.is_sensitive
        assert not v.requires_parent_alert
        assert not v.should_encrypt

    def test_whitespace_only_is_daily(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("   \n\t  ")
        assert v.tier == PrivacyTier.DAILY

    def test_benign_question_is_daily(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("为什么天空是蓝色的")
        assert v.tier == PrivacyTier.DAILY
        assert v.matched_category is None

    def test_homework_is_daily(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("帮我算一下 23 加 45 等于多少")
        assert v.tier == PrivacyTier.DAILY


class TestMildNegativeTier:
    """MILD_NEGATIVE: child expressed mild negative emotion."""

    def test_cn_sad_is_mild_negative(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("我今天心情不好")
        assert v.tier == PrivacyTier.MILD_NEGATIVE
        assert v.is_sensitive
        assert not v.requires_parent_alert

    def test_cn_annoyed_is_mild_negative(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("今天被同学烦死了")
        assert v.tier == PrivacyTier.MILD_NEGATIVE

    def test_en_sad_is_mild_negative(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("I feel sad today")
        assert v.tier == PrivacyTier.MILD_NEGATIVE


class TestHighRiskTier:
    """HIGH_RISK: self-harm or serious content triggers immediate alert."""

    def test_self_harm_cn_is_high_risk(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("我想自杀")
        assert v.tier == PrivacyTier.HIGH_RISK
        assert v.requires_parent_alert
        assert v.is_sensitive
        assert "self_harm" in (v.matched_category or "")

    def test_self_harm_want_to_die_is_high_risk(
        self,
        classifier: PrivacyTierClassifier,
    ) -> None:
        v = classifier.classify("我活不下去了")
        assert v.tier == PrivacyTier.HIGH_RISK

    def test_violence_is_high_risk(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("我想打死他")
        assert v.tier == PrivacyTier.HIGH_RISK
        assert v.requires_parent_alert

    def test_drugs_is_high_risk(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("什么是毒品")
        # Note: this can also be TREEHOLE_PRIVATE if phrasing is confidential.
        assert v.tier in (PrivacyTier.HIGH_RISK, PrivacyTier.TREEHOLE_PRIVATE)


class TestTreeholeTier:
    """TREEHOLE_PRIVATE: child requested confidentiality."""

    def test_secret_phrase_is_treehole(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("这是一个秘密")
        assert v.tier == PrivacyTier.TREEHOLE_PRIVATE
        assert v.should_encrypt
        assert v.is_sensitive

    def test_dont_tell_is_treehole(self, classifier: PrivacyTierClassifier) -> None:
        v = classifier.classify("不要告诉爸爸妈妈")
        assert v.tier == PrivacyTier.TREEHOLE_PRIVATE

    def test_treehole_overrides_mild_negative(
        self,
        classifier: PrivacyTierClassifier,
    ) -> None:
        """Treehole takes precedence over mild-negative signals.

        Order matters — most-severe wins.
        """
        text = "我心情不好，这是我们的秘密"
        v = classifier.classify(text)
        assert v.tier == PrivacyTier.TREEHOLE_PRIVATE

    def test_disable_treehole_falls_through(
        self,
    ) -> None:
        """With treehole disabled, '秘密' text falls through to DAILY."""
        clf = PrivacyTierClassifier(enable_treehole=False)
        v = clf.classify("这是一个秘密")
        assert v.tier == PrivacyTier.DAILY


class TestSeverityOrdering:
    """The classifier picks the most-severe tier when signals overlap."""

    def test_high_risk_beats_mild_negative(
        self,
        classifier: PrivacyTierClassifier,
    ) -> None:
        text = "我心情不好，我想自杀"
        v = classifier.classify(text)
        assert v.tier == PrivacyTier.HIGH_RISK

    def test_treehole_beats_high_risk(
        self,
        classifier: PrivacyTierClassifier,
    ) -> None:
        # Treehole keywords in the message plus high-risk content.
        # Per design, treehole takes priority (we don't escalate if
        # child asked for confidentiality).
        text = "这是我俩的秘密，我活不下去了"
        v = classifier.classify(text)
        assert v.tier == PrivacyTier.TREEHOLE_PRIVATE


class TestVerdictHelpers:
    """Properties on PrivacyTierVerdict mirror downstream policy."""

    def test_is_sensitive_true_for_mild_negative(self) -> None:
        v = PrivacyTierVerdict(tier=PrivacyTier.MILD_NEGATIVE)
        assert v.is_sensitive is True

    def test_is_sensitive_false_for_daily(self) -> None:
        v = PrivacyTierVerdict(tier=PrivacyTier.DAILY)
        assert v.is_sensitive is False

    def test_requires_parent_alert_only_for_high_risk(self) -> None:
        for tier in PrivacyTier:
            v = PrivacyTierVerdict(tier=tier)
            assert v.requires_parent_alert is (tier == PrivacyTier.HIGH_RISK)

    def test_should_encrypt_only_for_treehole(self) -> None:
        for tier in PrivacyTier:
            v = PrivacyTierVerdict(tier=tier)
            assert v.should_encrypt is (tier == PrivacyTier.TREEHOLE_PRIVATE)


class TestPickMostSensitive:
    """Fusing multiple agent outputs uses pick_most_sensitive."""

    def test_pick_empty_returns_daily(self) -> None:
        clf = PrivacyTierClassifier()
        result = clf.pick_most_sensitive([])
        assert result.tier == PrivacyTier.DAILY

    def test_pick_picks_highest_tier(self) -> None:
        clf = PrivacyTierClassifier()
        verdicts = [
            PrivacyTierVerdict(tier=PrivacyTier.DAILY),
            PrivacyTierVerdict(tier=PrivacyTier.HIGH_RISK),
            PrivacyTierVerdict(tier=PrivacyTier.MILD_NEGATIVE),
        ]
        result = clf.pick_most_sensitive(verdicts)
        assert result.tier == PrivacyTier.HIGH_RISK

    def test_pick_includes_treehole(self) -> None:
        clf = PrivacyTierClassifier()
        verdicts = [
            PrivacyTierVerdict(tier=PrivacyTier.HIGH_RISK),
            PrivacyTierVerdict(tier=PrivacyTier.TREEHOLE_PRIVATE),
        ]
        result = clf.pick_most_sensitive(verdicts)
        assert result.tier == PrivacyTier.TREEHOLE_PRIVATE
