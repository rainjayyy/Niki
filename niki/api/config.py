"""Client configuration endpoint (BE-MISC-001 + BE-MISC-002).

Returns a JSON config object the frontend should consume at
startup instead of hardcoding constants. v1.0 returns a single
static blob; v1.1+ will filter by client_type / version.

BE-MISC-002: feature flags now come from
`Settings.feature_flags_default` rather than being hardcoded —
keeps marketing-friendly knobs out of the code path. Callers
that need to flip flags at runtime should use
`niki.feature_flags.FeatureFlagService`.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from niki.feature_flags import (
    FeatureFlagService,
    get_feature_flag_service,
)

import structlog

logger = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/config",
    tags=[
        {
            "name": "config",
            "description": "Client configuration endpoint (BE-MISC-001 + BE-MISC-002).",
        }
    ],
)


# Non-flag defaults — static across versions and deploys.
# Feature flags live on the FeatureFlagService so they can be
# toggled without a deploy (BE-MISC-002).
DEFAULT_CLIENT_CONFIG: dict = {
    "api_version": "1.0.0",
    "min_supported_version": "1.0.0",
    # Session limits
    "daily_limit_minutes": 60,
    "session_max_idle_minutes": 10,
    "soft_warning_lead_minutes": 5,
    # Deletion cooldown
    "cooldown_days": 7,
    # Push channels enabled by default
    "enabled_channels": [
        "hms",
        "mipush",
        "oppo",
        "vivo",
        "flyme",
        "apns",
        "fcm",
        "web",
    ],
}


def _resolve_feature_flags(
    service: FeatureFlagService | None = None,
) -> dict:
    """Build the `feature_flags` payload from settings + overrides.

    Two layers of truth:

    1. `Settings.feature_flags_default` (JSON string) — the
       compile-time defaults.
    2. The in-process `FeatureFlagService`'s mutable dict — any
       runtime overrides applied via `set_override()`.

    The endpoint merges both: defaults act as the fallback,
    runtime overrides win. Anything missing in defaults is
    reported as `False` (the conservative answer for "is this
    feature on?").
    """
    svc = service or get_feature_flag_service()
    snapshot = svc.all_flags()
    # The service already merges defaults + runtime overrides;
    # `all_flags()` returns the full current view.
    return {name: bool(value) for name, value in sorted(snapshot.items())}


@router.get(
    "/client",
    summary="Return the client-facing runtime config",
    description="""
            Return the client-facing runtime config.
            
            v1.0 ignores `client_type` / `version` and returns the
            same blob to everyone. v1.1 will filter by these.
            """,
)
async def get_client_config(
    client_type: str | None = Query(
        None,
        description="e.g. web | weapp | ios | android | flutter",
    ),
    version: str | None = Query(
        None,
        description="Frontend X-Client-Version, semver",
    ),
) -> dict:
    """Return the client-facing runtime config.

    v1.0 ignores `client_type` / `version` and returns the
    same blob to everyone. v1.1 will filter by these.
    """
    try:
        feature_flags = _resolve_feature_flags()
    except ValueError as exc:
        # Malformed JSON in settings — keep the endpoint useful
        # rather than 500'ing the client at startup.
        logger.error(
            "config.feature_flags_parse_failed",
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "FEATURE_FLAGS_UNAVAILABLE",
                "message": ("feature_flags_default in settings is invalid; see server logs."),
            },
        ) from exc

    payload = dict(DEFAULT_CLIENT_CONFIG)
    payload["feature_flags"] = feature_flags
    payload["_query"] = {
        "client_type": client_type,
        "version": version,
    }
    return payload
