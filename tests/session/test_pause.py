"""Tests for the remote pause controller."""

import pytest

from niki.session import (
    NaturalExitMessage,
    RemotePauseController,
    SessionLimiter,
)


@pytest.fixture
def pause() -> RemotePauseController:
    limiter = SessionLimiter()
    return RemotePauseController(limiter)


class TestPauseRequest:
    def test_no_pause_initially(self, pause: RemotePauseController) -> None:
        assert not pause.has_pending_pause("c1")

    def test_request_pause(self, pause: RemotePauseController) -> None:
        pause.request_pause("c1", "p1")
        assert pause.has_pending_pause("c1")

    def test_consume_pause(self, pause: RemotePauseController) -> None:
        pause.request_pause("c1", "p1")
        msg = pause.consume_pause("c1")
        assert isinstance(msg, NaturalExitMessage)
        assert not pause.has_pending_pause("c1")  # Consumed

    def test_consume_pause_no_pending(self, pause: RemotePauseController) -> None:
        assert pause.consume_pause("c1") is None


class TestPauseMessage:
    def test_message_is_natural(self, pause: RemotePauseController) -> None:
        """PRD #33: child must NOT know parent intervened."""
        pause.request_pause("c1", "p1")
        msg = pause.consume_pause("c1")
        assert msg is not None
        # Should be a natural "Niki is tired" message
        # Should NOT mention "parent" or "user"
        for word in ["parent", "妈妈", "爸爸", "你妈妈", "你爸"]:
            assert word not in msg.text.lower()

    def test_message_does_not_show_summary(self, pause: RemotePauseController) -> None:
        """Pause messages don't include 'today's summary' (would reveal intervention)."""
        pause.request_pause("c1", "p1")
        msg = pause.consume_pause("c1")
        assert msg is not None
        # No summary on parent pause (show_summary=False)
        assert msg.show_summary is False


class TestNaturalExit:
    def test_natural_exit_includes_summary(self, pause: RemotePauseController) -> None:
        msg = pause.build_natural_exit("c1", topics_visited=["恐龙", "月亮"])
        assert msg.show_summary is True
        assert "恐龙" in msg.summary_topics
        assert "月亮" in msg.summary_topics

    def test_natural_exit_message_is_chinese(self, pause: RemotePauseController) -> None:
        msg = pause.build_natural_exit("c1")
        # Should be a friendly Chinese message
        assert any("一" <= c <= "鿿" for c in msg.text)

    def test_natural_exit_positive_tone(self, pause: RemotePauseController) -> None:
        """PRD #23: positive-first, never anxiety-inducing."""
        msg = pause.build_natural_exit("c1")
        # Should mention happiness or farewell
        assert any(w in msg.text for w in ["开心", "再见", "明天", "休息", "好"])

    def test_natural_exit_unique_per_topic_count(self, pause: RemotePauseController) -> None:
        msg1 = pause.build_natural_exit("c1", topics_visited=["a"])
        msg2 = pause.build_natural_exit("c1", topics_visited=["a", "b"])
        # Different topic counts → may pick different messages
        assert isinstance(msg1, NaturalExitMessage)
        assert isinstance(msg2, NaturalExitMessage)


class TestPauseState:
    def test_paused_state(self, pause: RemotePauseController) -> None:
        assert not pause.is_paused("c1")
        pause.request_pause("c1", "p1")
        pause.consume_pause("c1")
        assert pause.is_paused("c1")

    def test_clear_pause(self, pause: RemotePauseController) -> None:
        pause.request_pause("c1", "p1")
        pause.consume_pause("c1")
        pause.clear_pause("c1")
        assert not pause.is_paused("c1")
        assert not pause.has_pending_pause("c1")

    def test_clear_pause_no_pause(self, pause: RemotePauseController) -> None:
        # Clearing when no pause is a no-op
        pause.clear_pause("never")
        assert not pause.is_paused("never")


class TestEndSessionByParent:
    def test_end_session_returns_stats(self, pause: RemotePauseController) -> None:
        pause.limiter.start_session("c1")
        stats = pause.end_session_by_parent("c1")
        assert stats is not None
        assert stats.exit_reason.value == "parent_pause"

    def test_end_session_no_session(self, pause: RemotePauseController) -> None:
        stats = pause.end_session_by_parent("never")
        assert stats is None
