from __future__ import annotations
from typing import Any

"""Authentication & consent - PRD #30, #36, #35.

Public surface:
    ParentAccount            - the parent record (id, name, status, etc.)
    AuthStatus               - enum: PENDING | VERIFIED | SUSPENDED | DELETED
    RealNameVerifier         - verifies real-name identity (PRD #30)
    FaceVerifier             - biometric face check (PRD #30)
    ConsentRecord            - electronic informed-consent record (PRD #30, #36)
    AccountLifecycle         - orchestrates signup + deletion (PRD #35)

AccountLifecycle / DeletionStatus are
imported lazily (via __getattr__)
to break an import cycle: AccountLifecycle depends on
niki.db.repositories, which depends on niki.auth.account (this module).
BE-STOR-001.
"""

from niki.auth.account import (
    AccountStatus,
    AuthError,
    ParentAccount,
)
from niki.auth.consent import ConsentManager, ConsentRecord
from niki.auth.face_verify import FaceVerificationResult, FaceVerifier
from niki.auth.real_name import RealNameVerificationResult, RealNameVerifier

__all__ = [
    "AccountStatus",
    "AuthError",
    "ParentAccount",
    "ConsentManager",
    "ConsentRecord",
    "FaceVerifier",
    "FaceVerificationResult",
    "AccountLifecycle",
    "DeletionStatus",
    "RealNameVerifier",
    "RealNameVerificationResult",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial lazy loader
    """Lazily import lifecycle types to avoid the auth <-> db cycle."""
    if name in ("AccountLifecycle", "DeletionStatus"):
        from niki.auth.lifecycle import AccountLifecycle, DeletionStatus

        return {"AccountLifecycle": AccountLifecycle, "DeletionStatus": DeletionStatus}[name]
    raise AttributeError(f"module 'niki.auth' has no attribute {name!r}")
