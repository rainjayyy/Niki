"""Main API router — aggregates all sub-routers."""

from fastapi import APIRouter

from niki.api.health import router as health_router

api_router = APIRouter(prefix="/api/v1")

# Health & monitoring
api_router.include_router(health_router)

# Future routers will be added here:
# api_router.include_router(chat_router)       # Child chat endpoints
# api_router.include_router(parent_router)     # Parent dashboard endpoints
# api_router.include_router(auth_router)       # Authentication endpoints
# api_router.include_router(admin_router)      # Admin & pipeline management
