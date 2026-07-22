"""Main API router — aggregates all sub-routers."""

from fastapi import APIRouter

from niki.api.auth import router as auth_router
from niki.api.emotion import router as emotion_router
from niki.api.encyclopedia import router as encyclopedia_router
from niki.api.health import router as health_router
from niki.api.orchestrator import router as chat_router
from niki.api.routing import router as routing_router
from niki.api.safety import router as safety_router
from niki.api.tutoring import router as tutoring_router

api_router = APIRouter(prefix="/api/v1")

# Health & monitoring
api_router.include_router(health_router)

# Safety pipeline (#1 vertical slice)
api_router.include_router(safety_router)

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

# Parent authentication (#7 — real-name + face + consent + deletion)
api_router.include_router(auth_router)

# Future routers will be added here:
# api_router.include_router(parent_router)     # Parent dashboard endpoints
# api_router.include_router(admin_router)      # Admin & pipeline management
