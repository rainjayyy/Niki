"""Face verification — PRD #30.

Confirms that the live face image matches the ID photo. Stub
implementation that accepts well-formed image URLs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from niki.auth.account import AuthError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FaceVerificationResult:
    """Result of a face verification attempt."""

    verified: bool
    confidence: float
    liveness_score: float          # 0.0 - 1.0 (liveness detection)
    provider: str
    error: str | None = None
    verified_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "confidence": self.confidence,
            "liveness_score": self.liveness_score,
            "provider": self.provider,
            "error": self.error,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }


class FaceVerifier:
    """Verifies face images against a stored reference.

    Production: would call 阿里云/腾讯云 face verification API.
    Here: stub that accepts any URL ending in .jpg/.png.
    """

    def __init__(
        self,
        provider_name: str = "stub-face-verifier",
        min_confidence: float = 0.85,
        min_liveness: float = 0.7,
    ) -> None:
        self.provider_name = provider_name
        self.min_confidence = min_confidence
        self.min_liveness = min_liveness

    async def verify(
        self,
        live_image_url: str,
        reference_image_url: str | None = None,
    ) -> FaceVerificationResult:
        """Verify a face image.

        Args:
            live_image_url: URL of the live face image (just captured).
            reference_image_url: URL of the reference image (e.g. ID photo).
                If None, just liveness-detection is run.

        Raises:
            AuthError: If inputs are obviously malformed.
        """
        if not live_image_url or not live_image_url.startswith(("http://", "https://")):
            raise AuthError(
                code="INVALID_IMAGE_URL",
                message="live_image_url must be an http(s) URL",
            )
        if reference_image_url is not None and not reference_image_url.startswith(("http://", "https://")):
            raise AuthError(
                code="INVALID_IMAGE_URL",
                message="reference_image_url must be an http(s) URL",
            )

        await asyncio.sleep(0)  # placeholder for async I/O

        # Stub: accept any URL ending in common image extensions
        confidence, liveness = self._compute_stub_scores(live_image_url)
        verified = (
            confidence >= self.min_confidence
            and liveness >= self.min_liveness
        )

        result = FaceVerificationResult(
            verified=verified,
            confidence=confidence,
            liveness_score=liveness,
            provider=self.provider_name,
            verified_at=datetime.utcnow() if verified else None,
            error=None if verified else "Face verification failed (stub)",
        )

        logger.info(
            "face.verify",
            verified=verified,
            confidence=confidence,
            liveness=liveness,
        )

        return result

    @staticmethod
    def _compute_stub_scores(url: str) -> tuple[float, float]:
        """Deterministic stub scores for testing."""
        if not url:
            return 0.0, 0.0
        # URL must look like a real image
        if url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return 0.95, 0.92
        return 0.4, 0.4  # too low to verify
