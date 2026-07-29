"""Tests for ViolationRateTracker (BE-AUTH-001-rollout-alerts)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.security.rate_tracker import ViolationRateTracker


class TestViolationRateTracker:
    def test_invalid_args_raise(self) -> None:
        with pytest.raises(ValueError):
            ViolationRateTracker(window_seconds=0, burst_count=3)
        with pytest.raises(ValueError):
            ViolationRateTracker(window_seconds=300, burst_count=0)

    def test_first_record_returns_one(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        assert t.record("p1") == 1

    def test_below_threshold_does_not_alert(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        t.record("p1")
        t.record("p1")
        assert t.count("p1") == 2
        assert t.should_alert("p1") is False

    def test_at_threshold_alerts(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        t.record("p1")
        t.record("p1")
        t.record("p1")
        assert t.count("p1") == 3
        assert t.should_alert("p1") is True

    def test_above_threshold_keeps_alerting(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        for _ in range(5):
            t.record("p1")
        assert t.should_alert("p1") is True
        assert t.count("p1") == 5

    def test_old_events_fall_out_of_window(self) -> None:
        t = ViolationRateTracker(
            window_seconds=60, burst_count=3
        )
        # Record 3 events 90 seconds ago.
        past = datetime.now(timezone.utc) - timedelta(seconds=90)
        for _ in range(3):
            t.record("p1", now=past)
        # New record — past events should be pruned first.
        new_count = t.record("p1")
        assert new_count == 1  # only the just-recorded event
        assert t.should_alert("p1") is False

    def test_separate_actors_are_independent(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        for _ in range(3):
            t.record("p1")
        assert t.should_alert("p1") is True
        assert t.should_alert("p2") is False
        assert t.count("p2") == 0

    def test_empty_actor_id_record_returns_zero(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        assert t.record("") == 0

    def test_reset_clears_specific_actor(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        t.record("p1")
        t.record("p2")
        t.reset("p1")
        assert t.count("p1") == 0
        assert t.count("p2") == 1

    def test_reset_all(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        t.record("p1")
        t.record("p2")
        t.reset()
        assert t.count("p1") == 0
        assert t.count("p2") == 0

    def test_count_for_unknown_actor(self) -> None:
        t = ViolationRateTracker(
            window_seconds=300, burst_count=3
        )
        assert t.count("unknown") == 0
        assert t.should_alert("unknown") is False