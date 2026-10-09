"""TwoFactorConfig — parent-side 2FA / TOTP state (BE-AUTH-007).

One row per parent. The TOTP secret is stored encrypted with Fernet
(see `niki.services.two_factor.TwoFactorService`); what actually
lives on disk is never the base32 plaintext.

Backup codes: a single bcrypt hash of the **set** is stored, plus
a JSON list `backup_codes_used` recording which indices have been
consumed. We never store the individual codes — once shown to the
user at enrollment they're lost from the system. Regenerating
issues a new hash + new codes.

`is_enabled` flips on after the first successful TOTP verification
(`verify_enrollment`); the row is created in disabled state at
`begin_enrollment`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from niki.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TwoFactorConfig(Base):
    """Per-parent 2FA / TOTP enrollment state."""

    __tablename__ = "two_factor_configs"

    parent_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Fernet-encrypted base32 secret. NEVER plaintext.
    secret_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-code bcrypt hashes, newline-joined. Each hash is 60
    # bytes so 10 fit comfortably under the 1024-byte column cap.
    # bcrypt's 72-byte input limit means we hash each 8-char code
    # individually rather than the joined set (which would be
    # 89 bytes). The JSON `backup_codes_used` column tracks
    # which indices have been consumed.
    backup_codes_hash: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    # JSON list of consumed backup-code indices (ints, 0-based).
    backup_codes_used: Mapped[list[int]] = mapped_column(
        "backup_codes_used",
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        default=list,
    )
    # The raw backup codes are kept in memory only, hashed immediately
    # before persistence. They are returned ONCE via
    # `verify_enrollment` and never stored plaintext.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        onupdate=_utc_now,
    )

    __table_args__ = (
        # Common lookup: "all enabled 2FA rows for the burst detector"
        Index(
            "ix_two_factor_configs_enabled",
            "is_enabled",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TwoFactorConfig parent_id={self.parent_id} "
            f"is_enabled={self.is_enabled} "
            f"used={len(self.backup_codes_used or [])}/10>"
        )

    def to_dict(self) -> dict[str, Any]:
        """Public-facing projection — NEVER includes the encrypted
        secret. Used by `GET /2fa/status` and tests."""
        return {
            "parent_id": self.parent_id,
            "is_enabled": self.is_enabled,
            "enabled_at": (self.enabled_at.isoformat() if self.enabled_at else None),
            "last_used_at": (self.last_used_at.isoformat() if self.last_used_at else None),
            "backup_codes_used": list(self.backup_codes_used or []),
            "backup_codes_remaining": max(0, 10 - len(self.backup_codes_used or [])),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
