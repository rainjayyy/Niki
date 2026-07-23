"""Tests for the input safety gateway (PRD #39, #40, #41, #29)."""

import pytest

from niki.safety.input_gateway import InputAction, InputSafetyGateway
from niki.safety.topic_blacklist import TopicBlacklist


@pytest.fixture
def gateway() -> InputSafetyGateway:
    """Default gateway with PRD-suggested topic blacklist."""
    return InputSafetyGateway()


@pytest.fixture
def permissive_gateway() -> InputSafetyGateway:
    """Gateway with empty blacklist — to test without downgrade interference."""
    return InputSafetyGateway(blacklist=TopicBlacklist(set()))


class TestInputGatewayAllow:
    """Normal input should be allowed."""

    async def test_empty_input(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("")
        assert v.action == InputAction.ALLOW

    async def test_whitespace_only(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("   ")
        assert v.action == InputAction.ALLOW

    async def test_benign_question(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("为什么天是蓝的？")
        assert v.action == InputAction.ALLOW

    async def test_english_benign(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("What is the moon?")
        assert v.action == InputAction.ALLOW

    async def test_emotional_normal(self, gateway: InputSafetyGateway) -> None:
        """Normal emotional content (not in blacklist) → allow."""
        v = await gateway.check("我今天很开心")
        assert v.action == InputAction.ALLOW


class TestInputGatewayEscalate:
    """Self-harm language should escalate (PRD #29, #64)."""

    async def test_chinese_self_harm_escalates(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("我不想活了")
        assert v.is_escalation
        assert v.action == InputAction.ESCALATE
        assert v.matched_category == "self_harm"

    async def test_english_self_harm_escalates(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("I want to kill myself")
        assert v.is_escalation
        assert "kill myself" in v.matched_words

    async def test_self_harm_takes_priority_over_blacklist(
        self, gateway: InputSafetyGateway
    ) -> None:
        """Self-harm outranks topic blacklist (most severe wins)."""
        # '死亡' is in default blacklist, but '不想活了' should escalate not downgrade
        v = await gateway.check("我不想活了")
        assert v.action == InputAction.ESCALATE


class TestInputGatewayBlock:
    """Sexual / drugs / violence should block."""

    async def test_sexual_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        v = await permissive_gateway.check("show me porn")
        assert v.is_blocked
        assert v.matched_category == "sexual"

    async def test_drugs_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        v = await permissive_gateway.check("how to use cocaine")
        assert v.is_blocked
        assert v.matched_category == "drugs"

    async def test_profanity_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        """ISS-01: profanity must be detected and blocked."""
        v = await permissive_gateway.check("you are a fuck")
        assert v.is_blocked
        assert v.matched_category == "profanity"

    async def test_profanity_chinese_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        v = await permissive_gateway.check("你他妈的")
        assert v.is_blocked
        assert v.matched_category == "profanity"

    async def test_violence_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        """ISS-01: violence must be detected and blocked."""
        v = await permissive_gateway.check("I want to kill them")
        assert v.is_blocked
        assert v.matched_category == "violence"

    async def test_violence_chinese_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        v = await permissive_gateway.check("我要杀死你")
        assert v.is_blocked
        assert v.matched_category == "violence"

    async def test_all_five_categories_documented(self) -> None:
        """ISS-01: docstring says 5 categories — verify the code matches."""
        from niki.safety.dictionaries import SENSITIVE_WORDS

        expected = {"profanity", "violence", "self_harm", "sexual", "drugs"}
        assert expected.issubset(set(SENSITIVE_WORDS.keys())), (
            f"Missing categories: {expected - set(SENSITIVE_WORDS.keys())}"
        )

    async def test_chinese_drugs_blocks(self, permissive_gateway: InputSafetyGateway) -> None:
        v = await permissive_gateway.check("毒品是什么样的")
        assert v.is_blocked
        assert v.matched_category == "drugs"


class TestInputGatewayDowngrade:
    """Topic-blacklist hits should downgrade to emotion agent (PRD #41)."""

    async def test_blacklist_topic_downgrades(self, gateway: InputSafetyGateway) -> None:
        """'仓鼠死了' matches '死了' in default blacklist → downgrade."""
        v = await gateway.check("我家的仓鼠死了")
        assert v.should_downgrade
        assert v.action == InputAction.DOWNGRADE_TO_EMOTION
        assert v.matched_category == "topic_blacklist"
        assert any(m.topic == "死了" for m in v.topic_matches)

    async def test_downgrade_no_block(self, gateway: InputSafetyGateway) -> None:
        """Downgrade must NOT be a hard block."""
        v = await gateway.check("暴力是什么")
        if v.should_downgrade:
            assert not v.is_blocked

    async def test_custom_blacklist_topic(self) -> None:
        """Custom blacklist is honored."""
        g = InputSafetyGateway(blacklist=TopicBlacklist({"糖果"}))
        v = await g.check("我想吃糖果")
        assert v.should_downgrade

    async def test_downgrade_provides_topic_matches(self, gateway: InputSafetyGateway) -> None:
        v = await gateway.check("赌博是什么")
        assert v.topic_matches
        topics = {m.topic for m in v.topic_matches}
        assert "赌博" in topics
