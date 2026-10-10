"""Tests for the per-child AnomalyTracker (BE-ISS-NORTHSTAR).

Covers:
- Opening on |z| > 1.5
- Closing on the next in-baseline record + recovery latency
- Peak Z update while ongoing
- No-op for missing child_id / missing z_score
- Metric integration (counter + histogram)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from niki.agents.anomaly_tracker import (
    AnomalyTracker,
    RecoveryResult,
    get_tracker,
    set_tracker,
)


@pytest.fixture(autouse=True)
def _isolated_tracker() -> None:
    """Each test gets a fresh tracker (no process-global leakage)."""
    set_tracker(None)
    yield
    set_tracker(None)


class _StubRegistry:
    """Minimal stand-in for MetricsRegistry that records .inc() /
    .labels() / .observe() calls so tests can assert."""

    def __init__(self) -> None:
        self.inc_calls: list[tuple[str, dict[str, str]]] = []
        self.observe_calls: list[tuple[str, float]] = []

    class _Counter:
        def __init__(self, outer: "_StubRegistry", name: str) -> None:
            self._outer = outer
            self._name = name

        def labels(self, **kwargs: str) -> "_Counter":
            self._labels = kwargs
            return self

        def inc(self, amount: float = 1.0) -> None:
            self._outer.inc_calls.append((self._name, dict(self._labels)))

    class _Histogram:
        def __init__(self, outer: "_StubRegistry", name: str) -> None:
            self._outer = outer
            self._name = name

        def observe(self, value: float) -> None:
            self._outer.observe_calls.append((self._name, value))

    def __getattr__(self, name: str):
        if name.endswith("_TOTAL"):
            return self._Counter(self, name)
        if name.endswith("_SECONDS"):
            return self._Histogram(self, name)
        raise AttributeError(name)


class TestOpenCloseLifecycle:
    """The tracker's primary contract: open on anomaly, close on
    the next in-baseline record + observe recovery latency."""

    def test_opens_on_high_z(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        transition, recovery = t.observe(
            "c1", z_score=2.5, valence=-0.5, today=datetime(2026, 7, 22, tzinfo=timezone.utc)
        )
        assert transition == "started"
        assert recovery is None
        assert "c1" in t.open_anomalies
        # Counter incremented
        assert any(name == "EMOTION_ANOMALIES_TOTAL" for name, _ in reg.inc_calls)

    def test_closes_on_next_inbaseline_record(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        t0 = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)
        # Open
        t.observe("c1", z_score=2.5, valence=-0.5, today=t0)
        # Recover 3 hours later
        t1 = t0 + timedelta(hours=3)
        transition, recovery = t.observe("c1", z_score=0.4, valence=0.2, today=t1)
        assert transition == "recovered"
        assert isinstance(recovery, RecoveryResult)
        assert recovery.recovery_seconds == pytest.approx(3 * 3600, rel=0.01)
        assert "c1" not in t.open_anomalies
        # Histogram observed
        assert ("EMOTION_RECOVERY_SECONDS", 3 * 3600) in reg.observe_calls

    def test_ongoing_updates_peak(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        t.observe("c1", z_score=2.0, valence=-0.4, today=datetime(2026, 7, 22, tzinfo=timezone.utc))
        t.observe(
            "c1", z_score=2.8, valence=-0.6, today=datetime(2026, 7, 22, 1, tzinfo=timezone.utc)
        )
        evt = t.open_anomalies["c1"]
        assert abs(evt.peak_z - 2.8) < 1e-9

    def test_skipped_without_child_id(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        transition, recovery = t.observe(None, z_score=2.5, today=datetime.now(timezone.utc))
        assert transition == "skipped"
        assert recovery is None
        assert reg.inc_calls == []

    def test_skipped_without_z_score(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        transition, recovery = t.observe("c1", z_score=None, today=datetime.now(timezone.utc))
        assert transition == "skipped"
        assert recovery is None

    def test_ok_when_no_open_event(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        transition, recovery = t.observe("c1", z_score=0.4, today=datetime.now(timezone.utc))
        assert transition == "ok"
        assert recovery is None
        # No metric side-effects for "ok"
        assert reg.observe_calls == []


class TestProcessWideSingleton:
    """The module-level ``get_tracker`` returns a process-wide
    instance and ``set_tracker`` swaps it (used by tests)."""

    def test_singleton_cached(self) -> None:
        t1 = get_tracker()
        t2 = get_tracker()
        assert t1 is t2

    def test_set_tracker_replaces(self) -> None:
        t1 = get_tracker()
        replacement = AnomalyTracker(threshold=0.5)
        set_tracker(replacement)
        assert get_tracker() is replacement
        set_tracker(None)
        # And the next get rebuilds
        assert get_tracker() is not replacement


class TestForceClose:
    """``force_close`` is used by the pipeline reset path."""

    def test_closes_with_default_now(self) -> None:
        reg = _StubRegistry()
        t = AnomalyTracker(threshold=1.5, metric_registry=reg)
        t.observe("c1", z_score=2.5, today=datetime.now(timezone.utc) - timedelta(hours=1))
        result = t.force_close("c1")
        assert isinstance(result, RecoveryResult)
        assert result.child_id == "c1"
        assert "c1" not in t.open_anomalies
        # Histogram observed with the (approximately) 1h value
        assert any(name == "EMOTION_RECOVERY_SECONDS" for name, _ in reg.observe_calls)

    def test_returns_none_when_no_open(self) -> None:
        t = AnomalyTracker()
        assert t.force_close("c-nothing") is None
