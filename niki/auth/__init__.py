"""Authentication & consent — PRD #30, #36, #35.

Public surface:
    ParentAccount            — the parent record (id, name, status, etc.)
    AuthStatus               — enum: PENDING | VERIFIED | SUSPENDED | DELETED
    RealNameVerifier         — verifies real-name identity (PRD #30)
    FaceVerifier             — biometric face check (PRD #30)
    ConsentRecord           — electronic informed-consent record (PRD #30, #36)
    AccountLifecycle         — orchestrates signup + deletion (PRD #35)
"""

from niki.auth.account import (
    AccountStatus,
    AuthError,
    ParentAccount,
)
from niki.auth.consent import ConsentManager, ConsentRecord
from niki.auth.face_verify import FaceVerificationResult, FaceVerifier
from niki.auth.lifecycle import AccountLifecycle, DeletionStatus
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
