"""Tests for consent management."""

import pytest

from niki.auth.account import AuthError
from niki.auth.consent import ConsentManager, ConsentType


@pytest.fixture
def consent() -> ConsentManager:
    return ConsentManager()


class TestConsentDocuments:
    def test_parent_document(self, consent: ConsentManager) -> None:
        doc = consent.get_parent_document()
        assert "知情同意书" in doc or "Niki" in doc
        assert consent.PARENT_DOC_VERSION in doc

    def test_child_document(self, consent: ConsentManager) -> None:
        doc = consent.get_child_document()
        assert "Niki" in doc
        assert consent.CHILD_DOC_VERSION in doc

    def test_documents_are_different(self, consent: ConsentManager) -> None:
        parent = consent.get_parent_document()
        child = consent.get_child_document()
        assert parent != child


class TestConsentSigning:
    def test_sign_parent_consent(self, consent: ConsentManager) -> None:
        record = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        assert record.parent_id == "p-001"
        assert record.consent_type == ConsentType.PARENT
        assert record.typed_signature == "张三"
        assert record.ip_address == "1.2.3.4"
        assert record.device_fingerprint == "abc123"
        assert record.document_hash  # Should be non-empty
        assert record.revoked is False

    def test_sign_child_consent(self, consent: ConsentManager) -> None:
        record = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.CHILD,
            typed_signature="李四",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        assert record.consent_type == ConsentType.CHILD

    def test_empty_signature_raises(self, consent: ConsentManager) -> None:
        with pytest.raises(AuthError) as exc_info:
            consent.sign(
                parent_id="p-001",
                consent_type=ConsentType.PARENT,
                typed_signature="",
                ip_address="1.2.3.4",
                device_fingerprint="abc123",
            )
        assert exc_info.value.code == "EMPTY_SIGNATURE"

    def test_long_signature_raises(self, consent: ConsentManager) -> None:
        with pytest.raises(AuthError) as exc_info:
            consent.sign(
                parent_id="p-001",
                consent_type=ConsentType.PARENT,
                typed_signature="a" * 31,
                ip_address="1.2.3.4",
                device_fingerprint="abc123",
            )
        assert exc_info.value.code == "SIGNATURE_TOO_LONG"

    def test_document_hash_is_consistent(self, consent: ConsentManager) -> None:
        """Same document version → same hash."""
        r1 = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        r2 = consent.sign(
            parent_id="p-002",
            consent_type=ConsentType.PARENT,
            typed_signature="李四",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        # Both parent-version → same document hash
        assert r1.document_hash == r2.document_hash

    def test_different_versions_have_different_hashes(self, consent: ConsentManager) -> None:
        r_parent = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        r_child = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.CHILD,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        assert r_parent.document_hash != r_child.document_hash


class TestConsentLookup:
    def test_get_record(self, consent: ConsentManager) -> None:
        r = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        fetched = consent.get(r.id)
        assert fetched == r

    def test_get_nonexistent_returns_none(self, consent: ConsentManager) -> None:
        assert consent.get("nonexistent") is None

    def test_records_for_parent(self, consent: ConsentManager) -> None:
        consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.CHILD,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        records = consent.records_for_parent("p-001")
        assert len(records) == 2


class TestConsentRevocation:
    def test_revoke_record(self, consent: ConsentManager) -> None:
        r = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        revoked = consent.revoke(r.id)
        assert revoked is not None
        assert revoked.revoked is True
        assert revoked.revoked_at is not None

    def test_revoke_nonexistent_returns_none(self, consent: ConsentManager) -> None:
        assert consent.revoke("nonexistent") is None


class TestConsentSerialisation:
    def test_to_dict(self, consent: ConsentManager) -> None:
        r = consent.sign(
            parent_id="p-001",
            consent_type=ConsentType.PARENT,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc123",
        )
        d = r.to_dict()
        assert d["parent_id"] == "p-001"
        assert d["consent_type"] == "parent"
        assert d["typed_signature"] == "张三"
        assert d["ip_address"] == "1.2.3.4"
        assert d["device_fingerprint"] == "abc123"
        assert d["revoked"] is False
