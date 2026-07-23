"""Tests for treehole activation detection."""

import pytest

from niki.treehole.activation import (
    TREEHOLE_TRIGGERS,
    should_activate_treehole,
)


class TestActivation:
    @pytest.mark.parametrize(
        "text",
        [
            "我有一个秘密",
            "小秘密告诉你",
            "不要告诉别人",
            "别告诉爸妈",
            "只想跟你说",
            "保密",
            "I have a secret",
            "don't tell mom",
        ],
    )
    def test_triggers_activate_treehole(self, text: str) -> None:
        assert should_activate_treehole(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "今天天气真好",
            "我喜欢恐龙",
            "妈妈给我买了个新玩具",
            "I went to the park today",
            "你叫什么名字",
        ],
    )
    def test_normal_text_does_not_activate(self, text: str) -> None:
        assert should_activate_treehole(text) is False

    def test_empty_text_no_activation(self) -> None:
        assert should_activate_treehole("") is False

    def test_triggers_set_has_required_keys(self) -> None:
        assert "秘密" in TREEHOLE_TRIGGERS
        assert "secret" in TREEHOLE_TRIGGERS
        assert "private" in TREEHOLE_TRIGGERS
