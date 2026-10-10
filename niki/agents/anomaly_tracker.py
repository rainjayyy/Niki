"""Per-child anomaly tracker — PRD North Star metric.

The system claim: "以「主动关怀后情绪恢复至基线时间」替代
「情绪识别准确率」作为系统北极星指标".

A child enters an anomaly when their daily mean emotion Z-score
(against the 7-day baseline) exceeds 1.5σ in absolute value. The
anomaly is considered "open" until the next record comes back
inside the threshold; the elapsed time is the recovery latency
and is what we measure.

State is held in-process keyed by ``child_id`` — the recovery
histogram is the source of truth for historical data, so we
don't need a DB table just to count "current open" anomalies.

The tracker is intentionally cheap:
- dict lookup per observe() — sub-µs
- one Counter increment on anomaly open
- one Histogram observation on recovery close
- no DB / no LLM / no I/O

The class is process-wide (singleton via :func:`get_tracker`) so
the metric is the same across all orchestrator instances in the
same process.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class AnomalyEvent:
    """An open anomaly for one child."""

    child_id: str
    started_at: datetime
    peak_z: float
    peak_valence: float
    peak_date: str


# Result kinds returned by ``observe``. Useful for callers that
# want to log the transition (e.g. "child-123 anomaly recovered
# after 3.5h").
AnomalyTransition = Literal[
    "started",  # new anomaly opened
    "ongoing",  # already open, updated peak
    "recovered",  # open anomaly closed
    "ok",  # no anomaly, no open event
    "skipped",  # child_id missing — nothing to do
]


@dataclass
class RecoveryResult:
    """Returned by :meth:`AnomalyTracker.observe` when an anomaly closes."""

    child_id: str
    started_at: datetime
    recovered_at: datetime
    recovery_seconds: float
    peak_z: float
    peak_valence: float


class AnomalyTracker:
    """Tracks per-child open-anomaly state + emits the recovery-time
    Prometheus histogram on close.

    The North Star metric is the recovery latency: every time an
    anomaly closes, we observe the elapsed seconds into the
    ``niki_emotion_recovery_seconds`` histogram. The mean /
    median / p95 of that histogram is the metric dashboards
    surface.

    Args:
        threshold: |z| > threshold opens an anomaly. Default
            1.5 (matches PRD #52 + ``EmotionBaseline`` default).
        metric_registry: optional metrics registry (defaults to
            :func:`niki.metrics.get_registry`). Tests can inject
            a stub to assert on observations.
    """

    def __init__(
        self,
        threshold: float = 1.5,
        metric_registry: Any | None = None,
    ) -> None:
        self._open: dict[str, AnomalyEvent] = {}
        self._threshold = threshold
        self._registry = metric_registry

    @property
    def open_anomalies(self) -> dict[str, AnomalyEvent]:
        """Read-only view of the currently-open anomalies (testing)."""
        return dict(self._open)

    def open_count(self) -> int:
        """Number of children currently in an open anomaly."""
        return len(self._open)

    def observe(
        self,
        child_id: str | None,
        z_score: float | None,
        *,
        valence: float | None = None,
        date: str | None = None,
        today: datetime | None = None,
    ) -> tuple[AnomalyTransition, RecoveryResult | None]:
        """Record a new observation for ``child_id``.

        Args:
            child_id: The child's id. ``None`` is a no-op
                (the baseline isn't per-child without an id).
            z_score: Today's Z-score vs the 7-day baseline.
                ``None`` is treated as "not an anomaly".
            valence: Today's valence (kept on the event for
                debugging + for the recovered-event payload).
            date: ISO date for the record (default: today UTC).
            today: Wall-clock for the record. Used to compute
                recovery latency. Defaults to ``now(UTC)``.

        Returns:
            ``(transition, recovery_result)``. ``recovery_result``
            is non-None only when ``transition == "recovered"``.
        """
        if child_id is None or z_score is None:
            return ("skipped", None)
        if today is None:
            from datetime import timezone

            today = datetime.now(timezone.utc)
        if date is None:
            date = today.date().isoformat()

        is_anomaly = abs(z_score) > self._threshold

        if is_anomaly:
            existing = self._open.get(child_id)
            if existing is None:
                self._open[child_id] = AnomalyEvent(
                    child_id=child_id,
                    started_at=today,
                    peak_z=z_score,
                    peak_valence=valence if valence is not None else -z_score,
                    peak_date=date,
                )
                self._inc_anomalies()
                logger.info(
                    "anomaly.started",
                    child_id=child_id,
                    z_score=round(z_score, 3),
                    date=date,
                )
                return ("started", None)
            # Update peak
            if abs(z_score) > abs(existing.peak_z):
                existing.peak_z = z_score
                existing.peak_valence = valence if valence is not None else existing.peak_valence
                existing.peak_date = date
            return ("ongoing", None)

        # Not an anomaly — close any open event.
        existing = self._open.pop(child_id, None)
        if existing is None:
            return ("ok", None)
        recovery_seconds = max(0.0, (today - existing.started_at).total_seconds())
        result = RecoveryResult(
            child_id=child_id,
            started_at=existing.started_at,
            recovered_at=today,
            recovery_seconds=recovery_seconds,
            peak_z=existing.peak_z,
            peak_valence=existing.peak_valence,
        )
        self._observe_recovery(recovery_seconds)
        logger.info(
            "anomaly.recovered",
            child_id=child_id,
            recovery_seconds=round(recovery_seconds, 1),
            peak_z=round(existing.peak_z, 3),
        )
        return ("recovered", result)

    def force_close(self, child_id: str) -> RecoveryResult | None:
        """Close any open anomaly for ``child_id`` without observing a
        new value. Returns the recovery result if one was open.
        Used by tests + by the pipeline reset path."""
        from datetime import timezone

        existing = self._open.pop(child_id, None)
        if existing is None:
            return None
        now = datetime.now(timezone.utc)
        recovery_seconds = max(0.0, (now - existing.started_at).total_seconds())
        result = RecoveryResult(
            child_id=child_id,
            started_at=existing.started_at,
            recovered_at=now,
            recovery_seconds=recovery_seconds,
            peak_z=existing.peak_z,
            peak_valence=existing.peak_valence,
        )
        self._observe_recovery(recovery_seconds)
        return result

    # ---- metric plumbing ----

    def _inc_anomalies(self) -> None:
        if self._registry is None:
            from niki.metrics import get_registry

            self._registry = get_registry()
        # Lazy attr lookup so tests that swap the registry after
        # construction still see the new counter.
        counter = getattr(self._registry, "EMOTION_ANOMALIES_TOTAL", None)
        if counter is not None:
            try:
                counter.labels(direction="negative").inc()
            except Exception:
                logger.exception("anomaly.metric_inc_failed")

    def _observe_recovery(self, seconds: float) -> None:
        if self._registry is None:
            from niki.metrics import get_registry

            self._registry = get_registry()
        hist = getattr(self._registry, "EMOTION_RECOVERY_SECONDS", None)
        if hist is not None:
            try:
                hist.observe(seconds)
            except Exception:
                logger.exception("anomaly.metric_observe_failed")


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_tracker: AnomalyTracker | None = None
_tracker_lock = threading.Lock()


def get_tracker() -> AnomalyTracker:
    """Return the process-wide :class:`AnomalyTracker` (lazy-built)."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            from niki.metrics import get_registry

            _tracker = AnomalyTracker(metric_registry=get_registry())
        return _tracker


def set_tracker(tracker: AnomalyTracker | None) -> None:
    """Override the process-wide tracker (tests). Pass ``None`` to
    clear the cache so the next ``get_tracker()`` rebuilds."""
    global _tracker
    with _tracker_lock:
        _tracker = tracker


__all__ = [
    "AnomalyEvent",
    "AnomalyTracker",
    "AnomalyTransition",
    "RecoveryResult",
    "get_tracker",
    "set_tracker",
]
