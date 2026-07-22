"""Real-name verification — PRD #30.

Validates the parent's name + national ID number against an external
identity service. Stub implementation (in-memory) for development.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from niki.auth.account import AuthError, validate_id_number

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RealNameVerificationResult:
    """Result of a real-name verification attempt."""

    verified: bool
    name: str
    id_number_masked: str
    confidence: float          # 0.0 - 1.0
    provider: str
    error: str | None = None
    verified_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "name": self.name,
            "id_number_masked": self.id_number_masked,
            "confidence": self.confidence,
            "provider": self.provider,
            "error": self.error,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
        }


class RealNameVerifier:
    """Verifies a parent's real-name identity.

    Production: would call a third-party IDaaS (e.g. 阿里云实名认证).
    Here: stub with deterministic rule-based verification so the pipeline
    can be exercised end-to-end.
    """

    # Common surnames used to detect "obviously fake" names
    _VALID_SURNAME_HINTS = re.compile(
        r"^[一-鿿]{1,30}$"  # 1-30 Chinese characters
    )

    def __init__(
        self,
        provider_name: str = "stub-id-verifier",
        min_confidence: float = 0.8,
    ) -> None:
        self.provider_name = provider_name
        self.min_confidence = min_confidence

    async def verify(
        self,
        name: str,
        id_number: str,
    ) -> RealNameVerificationResult:
        """Verify a name + ID number.

        Raises:
            AuthError: If inputs are obviously malformed.
        """
        # 1. Format validation
        if not self._VALID_SURNAME_HINTS.match(name):
            raise AuthError(
                code="INVALID_NAME_FORMAT",
                message="Name must be 1-30 Chinese characters",
            )

        if not validate_id_number(id_number):
            raise AuthError(
                code="INVALID_ID_FORMAT",
                message="ID number must be 18 chars, last digit/X",
            )

        # 2. Stub provider logic — accepts most valid-looking inputs
        # Production: would call external IDaaS and wait for response
        await asyncio.sleep(0)  # placeholder for async I/O

        # In the stub: any well-formed ID verifies with high confidence
        confidence = self._compute_stub_confidence(name, id_number)
        verified = confidence >= self.min_confidence

        # Always mask in result
        masked = self._mask_id(id_number)

        result = RealNameVerificationResult(
            verified=verified,
            name=name,
            id_number_masked=masked,
            confidence=confidence,
            provider=self.provider_name,
            verified_at=datetime.utcnow() if verified else None,
            error=None if verified else "ID verification failed (stub)",
        )

        logger.info(
            "real_name.verify",
            name=name,
            id_masked=masked,
            verified=verified,
            confidence=confidence,
        )

        return result

    @staticmethod
    def _compute_stub_confidence(name: str, id_number: str) -> float:
        """Deterministic stub confidence — production would use external API.

        Returns 0.95 for well-formed inputs, 0.0 for empty.
        """
        if not name or not id_number:
            return 0.0
        # Deterministic "looks like a real ID" check
        if len(name) < 2:
            return 0.5
        return 0.95

    @staticmethod
    def _mask_id(id_number: str) -> str:
        """Mask an ID number: 110101****1234."""
        if len(id_number) < 8:
            return "****"
        return f"{id_number[:6]}****{id_number[-4:]}"
