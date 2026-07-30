"""Client configuration endpoint (BE-MISC-001).

Returns a JSON config object the frontend should consume at
startup instead of hardcoding constants. v1.0 returns a single
static blob; v1.1+ will filter by client_type / version.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

import structlog

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/config", tags=["config"])


# v1.0: single global config. Future tickets (BE-MISC-002) will
# split this into per-tenant configs and feature flags.
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
        "hms", "mipush", "oppo", "vivo", "flyme",
        "apns", "fcm", "web",
    ],
    # Feature flags
    "feature_flags": {
        "new_dashboard": True,
        "ocr_enabled": False,
        "sse_alerts": True,
    },
}


@router.get("/client")
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
    payload = dict(DEFAULT_CLIENT_CONFIG)
    payload["_query"] = {
        "client_type": client_type,
        "version": version,
    }
    return payload
