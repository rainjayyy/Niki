"""Main API router — aggregates all sub-routers."""

from fastapi import APIRouter

from niki.api.alerts import router as alerts_router
from niki.api.admin_audit import router as admin_audit_router
from niki.api.audio import router as audio_router
from niki.api.auth import router as auth_router
from niki.api.auth_audit import router as auth_audit_router
from niki.api.chat_stream import router as chat_stream_router
from niki.api.content_policy import router as content_policy_router
from niki.api.dashboard import router as dashboard_router
from niki.api.config import router as config_router
from niki.api.status import router as status_router
from niki.api.children import router as children_router
from niki.api.child_data import router as child_data_router
from niki.api.data_export import router as data_export_router
from niki.api.deletion import router as deletion_router
from niki.api.emotion import router as emotion_router
from niki.api.encyclopedia import router as encyclopedia_router
from niki.api.health import router as health_router
from niki.api.media import router as media_router
from niki.api.letters import router as letters_router
from niki.api.memory import router as memory_router
from niki.api.metrics import router as metrics_router
from niki.api.notifications import router as notifications_router
from niki.api.orchestrator import router as chat_router
from niki.api.pairing import router as pairing_router
from niki.api.push import router as push_router
from niki.api.resilience import router as resilience_router
from niki.api.routing import router as routing_router
from niki.api.safety import chat_router as safety_chat_router
from niki.api.safety import router as safety_router
from niki.api.two_factor import router as two_factor_router
from niki.api.session import router as session_router
from niki.api.tools import router as tools_router
from niki.api.treehole import router as treehole_router
from niki.api.tutoring import router as tutoring_router
from niki.api.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api/v1")


def _collect_tag_metadata(*routers) -> list[dict]:
    """Pull `{name, description}` entries from each router's tags list.

    FastAPI lets us attach tag metadata either inline (each `tags`
    entry is a dict) or globally via `app.openapi_tags`. We do both:
    per-router so Swagger UI / ReDoc show the description next to
    each tag group, and globally so a /openapi.json consumer can
    surface the same descriptions in tooltips.

    Routers' `tags` are sometimes plain strings (legacy) and
    sometimes dicts (BE-API-006 sweep). We unify them here.
    """
    seen: dict[str, str] = {}
    for router in routers:
        for tag in getattr(router, "tags", []) or []:
            if isinstance(tag, dict):
                name = tag.get("name")
                description = tag.get("description") or ""
                if name:
                    seen[name] = description
            elif isinstance(tag, str):
                seen.setdefault(tag, "")
    return [
        {"name": name, "description": description} for name, description in sorted(seen.items())
    ]


# BE-API-006: aggregated tag metadata, used by `create_app()` to
# populate `app.openapi_tags` (the global `tags` block in
# `/openapi.json`). Order doesn't matter — `openapi` reorders by
# appearance.
OPENAPI_TAGS: list[dict] = _collect_tag_metadata(
    health_router,
    safety_router,
    safety_chat_router,
    audio_router,
    media_router,
    letters_router,
    routing_router,
    encyclopedia_router,
    tutoring_router,
    emotion_router,
    chat_router,
    chat_stream_router,
    auth_router,
    dashboard_router,
    memory_router,
    resilience_router,
    treehole_router,
    session_router,
    content_policy_router,
    tools_router,
    data_export_router,
    children_router,
    pairing_router,
    auth_audit_router,
    admin_audit_router,
    alerts_router,
    deletion_router,
    config_router,
    status_router,
    push_router,
    notifications_router,
    webhooks_router,
    two_factor_router,
)

# Health & monitoring
api_router.include_router(health_router)

# Safety pipeline (#1 vertical slice)
api_router.include_router(safety_router)

# BE-MEDIA-002: Audio (ASR + future TTS)
api_router.include_router(audio_router)

# BE-MEDIA-001: Generic file upload
api_router.include_router(media_router)

# Parent-to-child letters (dashboard send + child inbox)
api_router.include_router(letters_router)

# Routing pipeline (#2 vertical slice)
api_router.include_router(routing_router)

# Encyclopedia Agent (#3 vertical slice)
api_router.include_router(encyclopedia_router)

# Tutoring Agent (#4 vertical slice)
api_router.include_router(tutoring_router)

# Emotion Agent (#5 vertical slice)
api_router.include_router(emotion_router)

# Chat orchestrator (#6 — composite intent end-to-end pipeline)
api_router.include_router(chat_router)

# BE-RT-001: SSE streaming
api_router.include_router(chat_stream_router)

# Parent authentication (#7 — real-name + face + consent + deletion)
api_router.include_router(auth_router)

# Parent dashboard (#8 — weekly reports + interest + learning + alerts)
api_router.include_router(dashboard_router)

# Memory system (#9 — short-term + long-term + graph + pipeline)
api_router.include_router(memory_router)

# Resilience (#10 — circuit breaker + model degradation)
api_router.include_router(resilience_router)

# Treehole mode (#11 — encrypted emotion summaries + 7-day auto-purge)
api_router.include_router(treehole_router)

# Session control (#12 — daily limit + remote pause + natural exit)
api_router.include_router(session_router)

# Content policy (#13 — family blacklist + age grading)
api_router.include_router(content_policy_router)

# Tool permission matrix (#14 — per-agent tool access control, PRD #54)
api_router.include_router(tools_router)

# BE-COMP-001: Data export (PIPL/GDPR data portability)
api_router.include_router(data_export_router)

# BE-STOR-001-ext-E: children registration
api_router.include_router(children_router)

# BE-STOR-001-ext-F: persistent child achievements / chat history / weekly report history
api_router.include_router(child_data_router)

# BE-DEV-004: parent-child pairing code
api_router.include_router(pairing_router)

# BE-AUTH-001-rollout-audit: lateral-auth audit log query
api_router.include_router(auth_audit_router)

# BE-COMP-004: unified cross-domain audit log query
api_router.include_router(admin_audit_router)

# BE-RT-002: realtime alert SSE stream
api_router.include_router(alerts_router)

# BE-COMP-005: deletion receipt PDF
api_router.include_router(deletion_router)

# BE-MISC-001: client runtime config
api_router.include_router(config_router)

# BE-MISC-003: public status page
api_router.include_router(status_router)

# BE-PUSH-001: push channel registration + preferences
api_router.include_router(push_router)

# BE-PUSH-005: parent-facing notification center
api_router.include_router(notifications_router)

# BE-API-009: Prometheus exposition endpoint (/metrics)
api_router.include_router(metrics_router)

# BE-API-008: webhook subscriptions + deliveries
api_router.include_router(webhooks_router)

# BE-AUTH-007: parent 2FA / TOTP endpoints (mount under /auth so
# the full path is /api/v1/auth/2fa/*).
api_router.include_router(two_factor_router)
