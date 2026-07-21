"""CORS middleware configuration."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from niki.config.settings import get_settings


def setup_cors(app: FastAPI) -> None:
    """Configure CORS middleware for the FastAPI application.

    In development, allows all origins. In production, restricts
    to the configured frontend domains.
    """
    settings = get_settings()

    origins = (
        ["*"]
        if settings.debug
        else [
            "http://localhost:3000",
            "http://localhost:5173",
        ]
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
