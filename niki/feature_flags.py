"""Feature flag service (BE-MISC-002).

A thin in-process wrapper around a mutable dict that:

    1. Loads global defaults from `Settings.feature_flags_default`
       (a JSON-encoded dict string — keeps the surface area small,
       no new DB schema, no external service like Unleash).
    2. Exposes `is_enabled(parent_id, flag)` — for v1 the
       `parent_id` is accepted but not yet used for per-parent
       overrides. The seam is here so A/B targeting can plug in
       without touching every call site.
    3. Provides a `set_override(flag, value)` method so tests and
       ops tools can flip a flag at runtime without restarting
       the process.

Per-parent overrides (BE-MISC-002 followup) will be backed by a
new table; for now the override applies to all parents
uniformly. That's an explicit design choice documented in the
ticket — see `docs/be-issues/BE-MISC-002.md`.
"""

from __future__ import annotations

import json
from threading import RLock
from typing import Any, Mapping

import structlog

logger = structlog.get_logger(__name__)


class FeatureFlagService:
    """Process-wide feature flag registry.

    Thread-safe via an internal `RLock` so concurrent
    request-handling threads can read flags while a background
    admin tool mutates them.
    """

    def __init__(
        self,
        defaults: Mapping[str, bool] | None = None,
        *,
        defaults_json: str | None = None,
    ) -> None:
        """Build a service.

        Prefer passing `defaults` (a plain dict). `defaults_json`
        is a convenience for the common case where the values
        come from `Settings.feature_flags_default` (a JSON string).
        Passing both raises `ValueError` — the caller must pick
        one source.
        """
        if defaults is not None and defaults_json is not None:
            raise ValueError(
                "pass either `defaults` or `defaults_json`, not both"
            )
        if defaults is not None:
            initial: dict[str, bool] = {k: bool(v) for k, v in dict(defaults).items()}
        elif defaults_json is not None:
            try:
                parsed = json.loads(defaults_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"feature_flags_default is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    "feature_flags_default must decode to a JSON object"
                )
            initial = {k: bool(v) for k, v in parsed.items()}
        else:
            initial = {}

        self._flags: dict[str, bool] = initial
        # Snapshot taken at construction so `reset()` can return
        # to the original defaults without requiring the caller to
        # hand them back in.
        self._initial: dict[str, bool] = dict(initial)
        self._lock = RLock()

    # ----- read API ----------------------------------------------------

    def is_enabled(self, parent_id: str | None, flag: str) -> bool:
        """Return whether `flag` is enabled for `parent_id`.

        v1 ignores `parent_id` — global flags only. The signature
        is deliberately parameterised so the call sites stay
        identical when per-parent overrides land.
        """
        # parent_id kept for future per-parent override table
        # lookup; current implementation consults only the
        # global dict.
        del parent_id
        with self._lock:
            return self._flags.get(flag, False)

    def all_flags(self) -> dict[str, bool]:
        """Return a snapshot of the current global flag dict."""
        with self._lock:
            return dict(self._flags)

    # ----- write API (admin / tests) ----------------------------------

    def set_override(self, flag: str, value: bool) -> None:
        """Set a flag at runtime.

        Used by tests and the (future) admin endpoint to toggle
        a flag without redeploying. Logs the change so audit
        trails pick it up via the log shipper.
        """
        with self._lock:
            previous = self._flags.get(flag)
            self._flags[flag] = bool(value)
            logger.info(
                "feature_flags.override",
                flag=flag,
                previous=previous,
                value=value,
            )

    def reset(self, defaults: Mapping[str, bool] | None = None) -> None:
        """Reset the registry to (new) defaults.

        `defaults=None` resets to the snapshot the service was
        constructed with — exposed so tests can undo their
        overrides between cases.
        """
        with self._lock:
            if defaults is None:
                self._flags = dict(self._initial)
                return
            coerced = {k: bool(v) for k, v in dict(defaults).items()}
            self._flags = coerced
            self._initial = dict(coerced)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_service: FeatureFlagService | None = None


def get_feature_flag_service() -> FeatureFlagService:
    """Return the process-wide FeatureFlagService, building defaults
    from settings on first access."""
    global _service
    if _service is None:
        from niki.config.settings import get_settings

        settings = get_settings()
        _service = FeatureFlagService(
            defaults_json=settings.feature_flags_default,
        )
    return _service


def set_feature_flag_service(service: FeatureFlagService) -> None:
    """Override the process-wide service (tests + admin tools)."""
    global _service
    _service = service


def reset_feature_flag_service_for_tests() -> None:
    """Drop the cached service so the next `get_*` rebuilds it.

    Test-only helper. Production code should never call this.
    """
    global _service
    _service = None


__all__ = [
    "FeatureFlagService",
    "get_feature_flag_service",
    "set_feature_flag_service",
    "reset_feature_flag_service_for_tests",
    # re-exported typing helper for callers that need to type-check
    "Mapping",
    "Any",
]
