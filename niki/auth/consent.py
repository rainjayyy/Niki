"""Electronic informed-consent — PRD #30, #36.

Records the parent's electronic signature on the informed-consent
document. Captures timestamp, IP, and device fingerprint for
non-repudiation.

PRD #36 requires two versions:
- 家长版 (parent version — full legal text)
- 儿童版 (child version — picture-based, age-appropriate)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

import structlog

from niki.auth.account import AuthError

logger = structlog.get_logger(__name__)


class ConsentType(StrEnum):
    """Types of consent documents."""

    PARENT = "parent"  # Full legal text
    CHILD = "child"  # Picture-based, age-appropriate


@dataclass(frozen=True)
class ConsentRecord:
    """A single signed consent record.

    Captures the legally-required evidence:
    - timestamp (server time)
    - IP address (captured at signing)
    - device fingerprint
    - document hash (proves the parent signed THIS version)
    - typed name (electronic signature)
    """

    id: str
    parent_id: str
    consent_type: ConsentType
    document_version: str
    document_hash: str  # SHA-256 of the signed document
    typed_signature: str  # Parent's typed name (e-signature)
    ip_address: str
    device_fingerprint: str
    signed_at: datetime
    revoked: bool = False
    revoked_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "consent_type": self.consent_type.value,
            "document_version": self.document_version,
            "document_hash": self.document_hash,
            "typed_signature": self.typed_signature,
            "ip_address": self.ip_address,
            "device_fingerprint": self.device_fingerprint,
            "signed_at": self.signed_at.isoformat(),
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }


class ConsentManager:
    """Manages consent signing and lookup.

    The signed record is non-repudiable evidence that the parent
    accepted the privacy policy at a specific time from a specific device.
    """

    PARENT_DOC_VERSION = "v1.0-parent-zh-CN"
    CHILD_DOC_VERSION = "v1.0-child-zh-CN"

    def __init__(self) -> None:
        self._records: dict[str, ConsentRecord] = {}

    def get_parent_document(self) -> str:
        """Return the parent-version consent document text.

        In production this would be loaded from a CMS or legal repo.
        Here: a hard-coded representative sample.
        """
        return (
            "Niki 儿童陪伴 AI 助手 — 家长知情同意书\n"
            f"版本 {self.PARENT_DOC_VERSION}\n\n"
            "本产品适合 3-12 岁儿童使用。\n"
            "我们承诺：\n"
            "1. 不会收集儿童不必要的个人信息；\n"
            "2. 原始音频不出设备；\n"
            "3. 树洞模式下的对话内容严格保密；\n"
            "4. 您可随时要求删除您孩子的全部数据（30 天内完成）。\n\n"
            "签署本协议即表示您已阅读并同意上述条款。"
        )

    def get_child_document(self) -> str:
        """Return the child-version consent document (picture-based, simplified)."""
        return (
            "🧒 Niki 陪你聊天啦！\n"
            f"版本 {self.CHILD_DOC_VERSION}\n\n"
            "🤖 Niki 不会问你的住址、电话、爸爸妈妈的信息哦！\n"
            "😊 你开心或难过都可以告诉 Niki！\n"
            "🔒 你和 Niki 的小秘密不会告诉别人！\n"
            "👨‍👩‍👧 如果你不想用了，爸爸妈妈可以帮你删除所有记录~"
        )

    def sign(
        self,
        parent_id: str,
        consent_type: ConsentType,
        typed_signature: str,
        ip_address: str,
        device_fingerprint: str,
    ) -> ConsentRecord:
        """Sign the consent document electronically.

        Args:
            parent_id: The signing parent's account id.
            consent_type: PARENT or CHILD.
            typed_signature: The parent's typed name (electronic signature).
            ip_address: IP address at signing time.
            device_fingerprint: Browser/device fingerprint.

        Returns:
            The signed ConsentRecord.

        Raises:
            AuthError: If the typed signature doesn't match the parent_id
                format (must be non-empty, ≤ 30 chars).
        """
        if not typed_signature or not typed_signature.strip():
            raise AuthError(
                code="EMPTY_SIGNATURE",
                message="typed_signature is required",
            )
        if len(typed_signature) > 30:
            raise AuthError(
                code="SIGNATURE_TOO_LONG",
                message="typed_signature must be ≤ 30 characters",
            )

        # Compute document hash (proves which version was signed)
        if consent_type == ConsentType.PARENT:
            document_text = self.get_parent_document()
            version = self.PARENT_DOC_VERSION
        else:
            document_text = self.get_child_document()
            version = self.CHILD_DOC_VERSION

        document_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()

        record = ConsentRecord(
            id=self._make_id(parent_id, consent_type),
            parent_id=parent_id,
            consent_type=consent_type,
            document_version=version,
            document_hash=document_hash,
            typed_signature=typed_signature,
            ip_address=ip_address,
            device_fingerprint=device_fingerprint,
            signed_at=datetime.now(timezone.utc),
        )

        self._records[record.id] = record
        logger.info(
            "consent.signed",
            parent_id=parent_id,
            consent_type=consent_type.value,
            document_hash=document_hash[:16],
        )
        return record

    def get(self, record_id: str) -> ConsentRecord | None:
        """Look up a consent record by id."""
        return self._records.get(record_id)

    def revoke(self, record_id: str) -> ConsentRecord | None:
        """Revoke a previously-signed consent (PRD right-to-withdraw)."""
        record = self._records.get(record_id)
        if record is None:
            return None
        # Mutate via dataclass replace (frozen)
        import dataclasses

        revoked = dataclasses.replace(
            record,
            revoked=True,
            revoked_at=datetime.now(timezone.utc),
        )
        self._records[record_id] = revoked
        return revoked

    def records_for_parent(self, parent_id: str) -> list[ConsentRecord]:
        """Get all consent records for a parent."""
        return [r for r in self._records.values() if r.parent_id == parent_id]

    def purge_for_parent(self, parent_id: str) -> int:
        """Erase all consent records for the given parent (ISS-04 / PRD #35).

        Returns:
            Number of records removed.
        """
        before = len(self._records)
        self._records = {
            rid: rec for rid, rec in self._records.items() if rec.parent_id != parent_id
        }
        return before - len(self._records)

    @staticmethod
    def _make_id(parent_id: str, consent_type: ConsentType) -> str:
        """Generate a deterministic consent record id."""
        raw = f"{parent_id}:{consent_type.value}:{datetime.now(timezone.utc).isoformat()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
