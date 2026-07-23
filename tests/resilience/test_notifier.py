"""Tests for the degradation notifier."""

import pytest

from niki.resilience.notifier import (
    DEGRADATION_MESSAGE,
    DegradationNotifier,
)


class TestDegradationNotifier:
    @pytest.fixture
    def notifier(self) -> DegradationNotifier:
        return DegradationNotifier()

    async def test_starts_inactive(self, notifier: DegradationNotifier) -> None:
        assert notifier.is_active() is False

    async def test_notify_active(self, notifier: DegradationNotifier) -> None:
        await notifier.notify_active("primary failed")
        assert notifier.is_active() is True
        assert notifier.state.reason == "primary failed"
        assert notifier.state.started_at is not None

    async def test_multiple_notifications_increment_count(
        self, notifier: DegradationNotifier
    ) -> None:
        await notifier.notify_active("a")
        await notifier.notify_active("b")
        await notifier.notify_active("c")
        assert notifier.state.notification_count == 3

    async def test_notify_recovered_resets_state(self, notifier: DegradationNotifier) -> None:
        await notifier.notify_active("a")
        await notifier.notify_recovered()
        assert notifier.is_active() is False
        assert notifier.state.reason == ""

    async def test_silent_message(self, notifier: DegradationNotifier) -> None:
        """PRD: message is positive + reassuring, not anxiety-inducing."""
        msg = notifier.get_message()
        # Positive language
        assert any(w in msg for w in ["正常", "放心", "调整"])
        # No panic words
        for w in ["crisis", "error", "down", "panic"]:
            assert w not in msg.lower()

    def test_state_to_dict(self, notifier: DegradationNotifier) -> None:
        d = notifier.state.to_dict()
        assert "active" in d
        assert "message" in d
        assert d["message"] == DEGRADATION_MESSAGE


class TestDegradationMessage:
    def test_message_is_chinese(self) -> None:
        # PRD: message should be in Chinese (parent-facing)
        assert any("一" <= c <= "鿿" for c in DEGRADATION_MESSAGE)
