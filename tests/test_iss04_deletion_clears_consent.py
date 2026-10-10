"""Tests for ISS-04: account deletion must clear consent records."""

from niki.auth import AccountLifecycle


class TestAccountDeletionClearsConsent:
    async def test_deletion_removes_consent_records(self) -> None:
        """ISS-04: signing consent + requesting deletion must actually
        remove the consent records (not just the account dict)."""
        lifecycle = AccountLifecycle()

        # Sign up + complete all 3 verifications
        account, _ = await lifecycle.start_signup(
            name="张三",
            id_number="110101199003078811",
            phone="13800138000",
        )
        await lifecycle.verify_real_name(
            parent_id=account.id,
            name="张三",
            id_number="110101199003078811",
        )
        await lifecycle.verify_face(
            parent_id=account.id,
            live_image_url="https://example.com/face.jpg",
        )
        await lifecycle.sign_consent(
            parent_id=account.id,
            typed_signature="张三",
            ip_address="1.2.3.4",
            device_fingerprint="abc",
        )

        # Both consent records (parent + child) should exist
        consent_records = lifecycle.get_consent_manager().records_for_parent(account.id)
        assert len(consent_records) == 2

        # Request and complete deletion
        await lifecycle.request_deletion(account.id)
        await lifecycle.complete_deletion(account.id)

        # ISS-04: consent records must be gone (the 30-day promise)
        remaining = lifecycle.get_consent_manager().records_for_parent(account.id)
        assert len(remaining) == 0, "ISS-04: consent records survived account deletion"

    async def test_deletion_does_not_touch_other_parents(self) -> None:
        lifecycle = AccountLifecycle()
        a, _ = await lifecycle.start_signup(
            name="A",
            id_number="110101199003078811",
            phone="13800138000",
        )
        b, _ = await lifecycle.start_signup(
            name="B",
            id_number="110101199003078812",
            phone="13800138001",
        )
        # Both sign consent
        for acc in (a, b):
            await lifecycle.sign_consent(
                parent_id=acc.id,
                typed_signature=acc.name,
                ip_address="1.2.3.4",
                device_fingerprint="abc",
            )

        # Delete only A
        await lifecycle.request_deletion(a.id)
        await lifecycle.complete_deletion(a.id)

        # B's consent should be intact
        b_consent = lifecycle.get_consent_manager().records_for_parent(b.id)
        assert len(b_consent) == 2
