"""Parent account model + auth errors.

PRD #30:
- 实名认证 (real-name verification)
- 人脸核身 (face verification)
- 电子知情同意书 (electronic informed-consent)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class AccountStatus(StrEnum):
    """Lifecycle status of a parent account."""

    PENDING = "pending"              # Signup started, verification incomplete
    VERIFIED = "verified"            # All 3 verifications passed
    SUSPENDED = "suspended"          # Manually suspended (e.g. fraud)
    DELETION_PENDING = "deletion_pending"  # Deletion requested, 30-day window
    DELETED = "deleted"              # Fully removed


class AuthError(Exception):
    """Base exception for auth failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# Validation helpers


_ID_NUMBER_RE = re.compile(r"^\d{17}[\dXx]$")
_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


def validate_id_number(id_number: str) -> bool:
    """Validate Chinese national ID format (18 chars, last is digit or X).

    Stub validation — real impl would check checksum.
    """
    return bool(_ID_NUMBER_RE.match(id_number))


def validate_phone(phone: str) -> bool:
    """Validate phone number format (10-15 digits, optional + prefix)."""
    return bool(_PHONE_RE.match(phone))


def mask_id_number(id_number: str) -> str:
    """Mask a national ID for safe logging (e.g. 110101****1234)."""
    if len(id_number) < 8:
        return "****"
    return f"{id_number[:6]}****{id_number[-4:]}"


def mask_phone(phone: str) -> str:
    """Mask a phone number for safe logging (e.g. 138****1234)."""
    if len(phone) < 7:
        return "****"
    return f"{phone[:3]}****{phone[-4:]}"


@dataclass
class ParentAccount:
    """A parent's account record.

    All sensitive fields (id_number, phone) are stored masked unless
    explicitly authorised.
    """

    id: str
    name: str
    id_number_masked: str          # For storage; raw never persisted
    phone_masked: str
    email: str | None = None
    email_verified: bool = False     # BE-AUTH-006
    status: AccountStatus = AccountStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    verified_at: datetime | None = None
    deletion_requested_at: datetime | None = None
    deletion_completed_at: datetime | None = None
    child_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        return self.status == AccountStatus.VERIFIED

    @property
    def deletion_window_remaining_days(self) -> int | None:
        """If deletion is pending, return days remaining in the 30-day window.

        PRD #35: account must be fully removed within 30 days.
        """
        if self.status != AccountStatus.DELETION_PENDING:
            return None
        if self.deletion_requested_at is None:
            return None
        # BE-STOR-001: tz-aware vs naive safety net. `deletion_requested_at`
        # may be aware (Postgres-backed) or naive (legacy in-memory);
        # align both sides before subtracting.
        requested = self.deletion_requested_at
        if requested.tzinfo is None:
            now = datetime.now(timezone.utc)
        else:
            from datetime import timezone as _tz

            now = datetime.now(_tz.utc)
        elapsed = now - requested
        remaining = 30 - elapsed.days
        return max(0, remaining)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for API response (always masked)."""
        return {
            "id": self.id,
            "name": self.name,
            "id_number_masked": self.id_number_masked,
            "phone_masked": self.phone_masked,
            "email": self.email,
            "email_verified": bool(self.email_verified),
            "status": self.status.value,
            "is_verified": self.is_verified,
            "created_at": self.created_at.isoformat(),
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "deletion_requested_at": (
                self.deletion_requested_at.isoformat()
                if self.deletion_requested_at
                else None
            ),
            "deletion_completed_at": (
                self.deletion_completed_at.isoformat()
                if self.deletion_completed_at
                else None
            ),
            "deletion_window_remaining_days": self.deletion_window_remaining_days,
            "child_ids": list(self.child_ids),
        }
