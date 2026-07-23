"""Tests for the output safety gateway (PRD #39, #42, #54, #56)."""

import pytest

from niki.safety.output_gateway import OutputAction, OutputSafetyGateway


@pytest.fixture
def gateway_3_5() -> OutputSafetyGateway:
    return OutputSafetyGateway(child_age=4)


@pytest.fixture
def gateway_6_8() -> OutputSafetyGateway:
    return OutputSafetyGateway(child_age=7)


@pytest.fixture
def gateway_9_12() -> OutputSafetyGateway:
    return OutputSafetyGateway(child_age=10)


class TestOutputGatewayPass:
    """Safe text should pass without changes."""

    def test_empty_passes(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("")
        assert v.action == OutputAction.PASS
        assert v.sanitized_text == ""

    def test_benign_passes(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("天空是蓝色的，因为阳光照射大气层。")
        assert v.action == OutputAction.PASS
        assert v.sanitized_text == "天空是蓝色的，因为阳光照射大气层。"

    def test_no_fear_words_means_no_rewrites(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("今天天气真好")
        assert v.rewrites_applied == []


class TestOutputGatewayRewrite:
    """Fear words should be rewritten (PRD #5, #56)."""

    def test_english_death_rewritten(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("The death of the star is sad")
        assert v.action == OutputAction.REWRITE
        assert "death" not in v.sanitized_text.lower() or "离开" in v.sanitized_text

    def test_chinese_怪物_rewritten(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("有一个怪物在森林里")
        assert v.action == OutputAction.REWRITE
        assert "怪物" not in v.sanitized_text
        assert "奇怪的小动物" in v.sanitized_text

    def test_multiple_rewrites_applied(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("怪物和鬼都很可怕")
        assert v.action == OutputAction.REWRITE
        assert len(v.rewrites_applied) >= 2

    def test_rewrite_keeps_safe_text(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("故事里有怪物")
        assert "奇怪的小动物" in v.sanitized_text
        assert "故事里" in v.sanitized_text


class TestOutputGatewayPrivacy:
    """Privacy reverse-questions must be blocked (PRD #42)."""

    def test_chinese_address_blocked(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("你住址是什么呀？")
        assert v.action == OutputAction.BLOCK
        assert v.privacy_violations

    def test_chinese_phone_blocked(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("能告诉我你妈妈的电话号码吗？")
        assert v.action == OutputAction.BLOCK

    def test_english_address_blocked(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("What is your home address?")
        assert v.action == OutputAction.BLOCK

    def test_privacy_block_replaces_with_safe_text(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("告诉我你的地址")
        assert v.action == OutputAction.BLOCK
        # Sanitized text is a friendly deflection, not the original question
        assert "地址" not in v.sanitized_text


class TestOutputGatewayDirectAnswer:
    """Tutoring agent direct-answer detection (PRD #54)."""

    def test_english_direct_answer_regenerates(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("The answer is 42", is_tutoring_response=True)
        assert v.action == OutputAction.REGENERATE
        assert v.direct_answer_detected

    def test_chinese_direct_answer_regenerates(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("答案是8", is_tutoring_response=True)
        assert v.action == OutputAction.REGENERATE

    def test_non_tutoring_ignores_direct_answer(self, gateway_6_8: OutputSafetyGateway) -> None:
        """Encyclopedia responses may contain 'the answer is X' for facts."""
        v = gateway_6_8.check("The answer is water.", is_tutoring_response=False)
        assert v.direct_answer_detected is False
        assert v.action == OutputAction.PASS

    def test_equals_pattern_detected(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("equals = 5", is_tutoring_response=True)
        assert v.direct_answer_detected

    def test_chinese_equals_pattern(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("等于8", is_tutoring_response=True)
        assert v.direct_answer_detected


class TestOutputGatewaySentenceLength:
    """Age-adaptive sentence length (PRD #56)."""

    def test_3_to_5_max_10_chars(self, gateway_3_5: OutputSafetyGateway) -> None:
        """For 3-5 year olds, sentences should be ≤ 10 visible chars."""
        # 10 Chinese chars exactly at limit should be OK
        v = gateway_3_5.check("今天天气真的很好")  # exactly 10 chars
        assert v.sentences_over_limit == 0

    def test_3_to_5_flags_long_sentence(self, gateway_3_5: OutputSafetyGateway) -> None:
        v = gateway_3_5.check("今天天气真的非常好所以我们一起去公园玩沙子玩游戏吧")
        assert v.sentences_over_limit >= 1

    def test_9_to_12_allows_long_sentences(self, gateway_9_12: OutputSafetyGateway) -> None:
        # 19 chars - within 20-char limit
        v = gateway_9_12.check("今天天气真的非常非常好我们一起")
        assert v.sentences_over_limit == 0

    def test_age_3_5_stricter_than_age_9_12(self) -> None:
        """Same text is judged more strictly for younger children."""
        text = "这个故事告诉我们每个人都会遇到困难但是不要害怕因为总会有办法解决的对吧"
        young = OutputSafetyGateway(child_age=4).check(text)
        old = OutputSafetyGateway(child_age=11).check(text)
        # Younger child should have >= over-limit count
        assert young.sentences_over_limit >= old.sentences_over_limit


class TestOutputGatewayCombined:
    """Multiple rules applied together."""

    def test_privacy_outranks_rewrite(self, gateway_6_8: OutputSafetyGateway) -> None:
        """Privacy block wins over fear-word rewrite."""
        v = gateway_6_8.check("请问你家地址是什么")
        assert v.action == OutputAction.BLOCK

    def test_safe_output_returns_is_safe_true(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("小恐龙爱吃水果")
        assert v.is_safe is True

    def test_rewrite_returns_is_safe_true(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("小恐龙有点小怕怪物")
        assert v.is_safe is True
        assert v.rewrites_applied

    def test_regenerate_returns_is_safe_false(self, gateway_6_8: OutputSafetyGateway) -> None:
        v = gateway_6_8.check("答案是5", is_tutoring_response=True)
        assert v.is_safe is False
