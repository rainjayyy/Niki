"""End-to-end API + service tests for the parent 2FA / TOTP feature (BE-AUTH-007).

Two halves:

  1. Service-level tests in `TestTwoFactorService` exercise the
     pure business logic (TOTP verify, backup-code match, replay
     prevention, encryption-at-rest, password-re-auth gate) via
     the `InMemoryTwoFactorService` directly so we can control
     time, repository state, and key material.

  2. Endpoint-level tests in `TestTwoFactorEndpoints` go through
     the FastAPI TestClient with the real router, asserting the
     full login-flow shape (SMS login → partial_token → 2FA
     login), the enrollment round-trip, the sensitive-op gate,
     and the rate-limit / audit-log wiring.

The test count is intentionally ≥ 18 to satisfy AC-9.
"""

from __future__ import annotations

import time
from typing import AsyncGenerator

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from niki.auth.jwt_service import (
    SCOPE_2FA_PENDING,
    SCOPE_FULL,
    decode_unverified,
    issue_access_token,
)
from niki.db.repositories import (
    InMemoryAuditLogRepository,
    InMemoryTwoFactorRepository,
    set_audit_log_repository,
    set_two_factor_repository,
)
from niki.main import create_app
from niki.services.audit_log import AuditLogService, set_audit_log_service
from niki.services.two_factor import (
    BACKUP_CODE_COUNT,
    InMemoryTwoFactorService,
    TwoFactorAlreadyEnrolled,
    TwoFactorBackupCodeInvalid,
    TwoFactorInvalidCode,
    TwoFactorNotEnrolled,
    TwoFactorReauthRequired,
    set_two_factor_service,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_two_factor_state() -> AsyncGenerator[None, None]:
    """Reset the 2FA + unified-audit singletons between tests so
    cross-test state doesn't leak. Auth + push singletons are
    reset by conftest already."""
    repo = InMemoryTwoFactorRepository()
    set_two_factor_repository(repo)
    audit_repo = InMemoryAuditLogRepository()
    set_audit_log_repository(audit_repo)
    set_audit_log_service(AuditLogService(repo=audit_repo))
    # Use a deterministic Fernet key so encryption-at-rest can
    # be inspected.
    fernet_key = Fernet.generate_key()
    svc = InMemoryTwoFactorService(
        repo=repo, fernet_key=fernet_key
    )
    set_two_factor_service(svc)
    yield
    set_two_factor_service(None)  # type: ignore[arg-type]
    set_two_factor_repository(None)  # type: ignore[arg-type]


@pytest.fixture
def svc() -> InMemoryTwoFactorService:
    """Convenience accessor for the autouse service fixture."""
    from niki.services.two_factor import get_two_factor_service
    return get_two_factor_service()  # type: ignore[return-value]


@pytest.fixture
def audit_repo() -> InMemoryAuditLogRepository:
    from niki.db.repositories import get_audit_log_repository
    return get_audit_log_repository()  # type: ignore[return-value]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient with NO fake-auth override installed.

    The conftest autouse fixture `_fake_auth_override` makes
    every `get_current_parent` return `test_parent` regardless
    of the JWT's `sub`. For 2FA tests we need each call to
    honor the JWT, so we explicitly unset `NIKI_TEST_FAKE_AUTH`
    before the app is created.
    """
    monkeypatch.delenv("NIKI_TEST_FAKE_AUTH", raising=False)
    return TestClient(create_app())


def _send_sms_code(phone: str) -> str:
    """Send an SMS code via the in-process service and return it.

    The dev_code echo in `/auth/sms/send` is only enabled when
    `Settings.debug` is True; tests don't depend on that flag
    here, so we lean on `InMemorySMSCodeService.send()` directly
    to retrieve the code. To avoid the 60s cooldown across the
    repeated `send()` calls in a single test, we reset the
    service singleton between calls so each one returns a fresh
    code.
    """
    import asyncio
    from niki.auth.sms_service import (
        InMemorySMSCodeService,
        get_sms_service,
        set_sms_service,
    )
    # Reset to a fresh service so the per-phone cooldown doesn't
    # block re-sends within the same test.
    fresh = InMemorySMSCodeService()
    set_sms_service(fresh)
    return asyncio.run(fresh.send(phone))


def _signup(client: TestClient, phone: str = "13800138000") -> str:
    """Helper: complete the signup start step; returns parent_id.

    The fake-auth override makes the parent_id in the JWT the
    test parent's id ("test_parent"), but the SMS-login path
    auto-creates a fresh account keyed on the phone hash, so we
    enroll 2FA on that fresh account, not on the override id.
    Tests that need to drive the 2FA flow directly can also use
    the service fixture (see `svc`) and bypass the API.
    """
    r = client.post(
        "/api/v1/auth/sms/send",
        json={"phone": phone},
    )
    assert r.status_code == 200
    dev_code = r.json()["dev_code"]
    r = client.post(
        "/api/v1/auth/sms/login",
        json={"phone": phone, "code": dev_code},
    )
    assert r.status_code == 200
    return r.json()["parent_id"]


def _send_and_login(client: TestClient, phone: str) -> str:
    """Send an SMS code, retrieve the in-memory code via the
    service singleton (since dev_code is only echoed in DEBUG),
    and complete the SMS login. Returns parent_id.

    `InMemorySMSCodeService.send()` returns the code directly,
    so we lean on it to avoid coupling tests to the debug flag.
    """
    code = _send_sms_code(phone)
    r = client.post(
        "/api/v1/auth/sms/login",
        json={"phone": phone, "code": code},
    )
    assert r.status_code == 200, r.text
    return r.json()["parent_id"]


def _future_totp(secret: str, *, offset_seconds: int = 31) -> str:
    """Return a TOTP code valid in a future 30-second window.

    Replay-prevention in the service records the latest
    accepted timecode per parent, so calling
    `pyotp.TOTP(secret).now()` immediately after enrollment
    returns a code that the service has already seen. Tests
    that need a *new* valid code must step forward at least
    one window (31 seconds) — hence this helper.
    """
    future_ts = int(time.time()) + offset_seconds
    return pyotp.TOTP(secret).at(for_time=future_ts)


# ===========================================================================
# Service-level tests
# ===========================================================================


class TestTwoFactorService:
    """Direct exercise of `InMemoryTwoFactorService`."""

    @pytest.mark.asyncio
    async def test_begin_enrollment_returns_secret_and_qr(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-1: enroll returns secret + otpauth URL + QR code.
        payload = await svc.begin_enrollment("p1")
        assert isinstance(payload.secret_plaintext, str)
        assert len(payload.secret_plaintext) >= 32
        assert payload.otpauth_url.startswith("otpauth://totp/")
        assert "secret=" in payload.otpauth_url
        assert payload.qr_code.startswith("<svg")
        assert "</svg>" in payload.qr_code

    @pytest.mark.asyncio
    async def test_verify_enrollment_enables_2fa_and_returns_codes(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-1: verify_enrollment returns backup codes + enables 2FA.
        payload = await svc.begin_enrollment("p1")
        code = pyotp.TOTP(payload.secret_plaintext).now()
        codes = await svc.verify_enrollment("p1", code)
        assert len(codes) == BACKUP_CODE_COUNT
        for c in codes:
            assert len(c) == 8
            # All characters from BACKUP_CODE_ALPHABET
            for ch in c:
                assert ch in (
                    "23456789"
                    "ABCDEFGHJKMNPQRSTUVWXYZ"
                    "abcdefghjkmnpqrstuvwxyz"
                )
        assert await svc.is_enabled("p1") is True

    @pytest.mark.asyncio
    async def test_verify_code_with_valid_totp_succeeds(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-2: valid TOTP within window passes.
        payload = await svc.begin_enrollment("p1")
        # Complete enrollment to flip is_enabled=True.
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        # Advance past the step verify_enrollment consumed.
        future_ts = int(time.time()) + 31  # +1 window
        future_code = pyotp.TOTP(
            payload.secret_plaintext
        ).at(for_time=future_ts)
        assert await svc.verify_code("p1", future_code) is True

    @pytest.mark.asyncio
    async def test_verify_code_with_wrong_totp_fails(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-2: wrong code raises TwoFactorInvalidCode.
        payload = await svc.begin_enrollment("p1")
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        with pytest.raises(TwoFactorInvalidCode):
            await svc.verify_code("p1", "000000")

    @pytest.mark.asyncio
    async def test_verify_code_rejects_replay_in_same_window(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-5: same code can't be reused within the same 30-s window.
        payload = await svc.begin_enrollment("p1")
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        future_ts = int(time.time()) + 31
        code = pyotp.TOTP(payload.secret_plaintext).at(
            for_time=future_ts
        )
        assert await svc.verify_code("p1", code) is True
        # Same code again — must fail.
        with pytest.raises(TwoFactorInvalidCode):
            await svc.verify_code("p1", code)

    @pytest.mark.asyncio
    async def test_verify_backup_code_succeeds_then_marks_used(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-3: backup code works, then is consumed.
        payload = await svc.begin_enrollment("p1")
        codes = await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        assert await svc.verify_backup_code("p1", codes[0]) is True
        status = await svc.status("p1")
        assert status["backup_codes_remaining"] == BACKUP_CODE_COUNT - 1

    @pytest.mark.asyncio
    async def test_backup_code_cannot_be_replayed(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-3: replaying a consumed backup code fails.
        payload = await svc.begin_enrollment("p1")
        codes = await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        await svc.verify_backup_code("p1", codes[0])
        with pytest.raises(TwoFactorBackupCodeInvalid):
            await svc.verify_backup_code("p1", codes[0])

    @pytest.mark.asyncio
    async def test_disable_requires_password_reauth(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-1: disable requires re-auth (empty token → fail).
        payload = await svc.begin_enrollment("p1")
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        with pytest.raises(TwoFactorReauthRequired):
            await svc.disable("p1", "")
        # Non-empty reauth passes
        assert await svc.disable("p1", "any-non-empty") is True
        assert await svc.is_enabled("p1") is False

    @pytest.mark.asyncio
    async def test_regenerate_backup_codes_invalidates_old_codes(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-3: regenerating invalidates the old set.
        payload = await svc.begin_enrollment("p1")
        old_codes = await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        new_codes = await svc.regenerate_backup_codes(
            "p1", "any-non-empty"
        )
        assert new_codes != old_codes
        # Old code no longer works.
        with pytest.raises(TwoFactorBackupCodeInvalid):
            await svc.verify_backup_code("p1", old_codes[0])
        # New code does.
        assert (
            await svc.verify_backup_code("p1", new_codes[0]) is True
        )

    @pytest.mark.asyncio
    async def test_secret_encrypted_at_rest(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-4: stored secret is NOT the plaintext.
        payload = await svc.begin_enrollment("p_secret_test")
        repo = await _get_repo()
        row = await repo.get("p_secret_test")
        assert row is not None
        assert row.secret_encrypted != payload.secret_plaintext
        # Fernet ciphertext is base64-ish and starts with
        # 'gAAA' (version byte 0x80 encoded). Loose match.
        assert row.secret_encrypted.startswith("gAAAAA")

    @pytest.mark.asyncio
    async def test_status_reflects_enrollment(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC-1: GET /2fa/status shows enrollment state.
        payload = await svc.begin_enrollment("p1")
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        status = await svc.status("p1")
        assert status["is_enabled"] is True
        assert status["enrolled_at"] is not None
        assert status["backup_codes_remaining"] == BACKUP_CODE_COUNT
        assert status["last_used_at"] is None  # not used yet

    @pytest.mark.asyncio
    async def test_double_enrollment_rejected(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC: a second begin_enrollment on an enabled account fails.
        payload = await svc.begin_enrollment("p1")
        await svc.verify_enrollment(
            "p1", pyotp.TOTP(payload.secret_plaintext).now()
        )
        with pytest.raises(TwoFactorAlreadyEnrolled):
            await svc.begin_enrollment("p1")

    @pytest.mark.asyncio
    async def test_verify_enrollment_before_begin_fails(
        self, svc: InMemoryTwoFactorService
    ) -> None:
        # AC: cannot verify enrollment without calling begin first.
        with pytest.raises(TwoFactorNotEnrolled):
            await svc.verify_enrollment("p_none", "123456")


async def _get_repo():
    from niki.db.repositories import get_two_factor_repository
    return get_two_factor_repository()  # type: ignore[return-value]


# ===========================================================================
# Endpoint-level tests
# ===========================================================================


class TestTwoFactorEndpoints:
    """Drive the FastAPI router through the TestClient."""

    def test_status_endpoint_disabled_by_default(
        self, client: TestClient
    ) -> None:
        # GET /2fa/status returns is_enabled=False for a parent
        # that hasn't enrolled.
        # Bypass the fake-auth override by directly using a real
        # JWT (the fake override only fires when NIKI_TEST_FAKE_AUTH
        # is set; we deliberately DO unset it here via the conftest
        # autouse fixture in tests/api/test_auth_audit.py).
        # Instead, rely on the in-test override via headers.
        # The simpler approach: hit the endpoint with a real
        # access token issued for a fresh parent_id.
        token = issue_access_token("p_status_test", scope=SCOPE_FULL)
        r = client.get(
            "/api/v1/auth/2fa/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_enabled"] is False
        assert body["enrolled_at"] is None
        assert body["backup_codes_remaining"] == 0

    def test_enroll_then_verify_enables_2fa(
        self, client: TestClient
    ) -> None:
        # AC-1 + AC-2: enroll → verify → status reports enabled.
        token = issue_access_token("p_enroll_test", scope=SCOPE_FULL)
        # Enroll
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "secret" in body
        assert "otpauth_url" in body
        assert "qr_code" in body

        # Verify
        code = pyotp.TOTP(body["secret"]).now()
        r = client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": code},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "enabled"
        assert len(body["backup_codes"]) == BACKUP_CODE_COUNT

        # Status
        r = client.get(
            "/api/v1/auth/2fa/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.json()["is_enabled"] is True

    def test_enroll_verify_wrong_code_returns_401(
        self, client: TestClient
    ) -> None:
        # AC: invalid TOTP at enrollment → 401.
        token = issue_access_token("p_enroll_wrong", scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        r = client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": "000000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "TWO_FA_INVALID_CODE"

    def test_verify_endpoint_returns_scope_token(
        self, client: TestClient
    ) -> None:
        # AC-1: POST /2fa/verify returns a full-scope scope_token.
        token = issue_access_token(
            "p_verify_test", scope=SCOPE_FULL
        )
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        r = client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        # Now hit /verify with a fresh TOTP from a future window.
        r = client.post(
            "/api/v1/auth/2fa/verify",
            json={"totp_code": _future_totp(secret)},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verified_via"] == "totp"
        assert "scope_token" in body
        # Decode to confirm scope=full_reauth (sensitive-op gate
        # trusts this scope; `full` does NOT satisfy the gate).
        claims = decode_unverified(body["scope_token"])
        from niki.auth.jwt_service import SCOPE_FULL_REAUTH
        assert claims["scope"] == SCOPE_FULL_REAUTH

    def test_login_with_2fa_enabled_returns_partial_token(
        self, client: TestClient
    ) -> None:
        # AC-1: when 2FA is enabled, sms/login returns 2fa_required.
        phone = "13800138001"
        # Step 1: SMS login to create the account.
        parent_id = _send_and_login(client, phone)
        # Step 2: enroll + verify 2FA on that account via service.
        # We use the service singleton because the fake-auth
        # override binds parent_id to "test_parent" — bypass
        # it by issuing a real token for the fresh parent.
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        r = client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        # Step 3: re-login → must return partial_token.
        code = _send_sms_code(phone)
        r = client.post(
            "/api/v1/auth/sms/login",
            json={"phone": phone, "code": code},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "2fa_required"
        assert "partial_token" in body
        claims = decode_unverified(body["partial_token"])
        assert claims["scope"] == SCOPE_2FA_PENDING

    def test_login_with_2fa_disabled_returns_full_tokens(
        self, client: TestClient
    ) -> None:
        # AC: existing behaviour preserved when 2FA is not enrolled.
        phone = "13800138002"
        code = _send_sms_code(phone)
        r = client.post(
            "/api/v1/auth/sms/login",
            json={"phone": phone, "code": code},
        )
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert "refresh_token" in body
        # No `status: 2fa_required` field — full tokens issued.
        assert body.get("status") != "2fa_required"

    def test_two_factor_login_with_valid_totp_returns_full_tokens(
        self, client: TestClient
    ) -> None:
        # AC-1: POST /2fa/login with valid TOTP returns full tokens.
        phone = "13800138003"
        parent_id = _send_and_login(client, phone)
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Re-login to get partial_token.
        code = _send_sms_code(phone)
        r = client.post(
            "/api/v1/auth/sms/login",
            json={"phone": phone, "code": code},
        )
        partial = r.json()["partial_token"]
        # Hit /2fa/login with the partial + a future TOTP.
        r = client.post(
            "/api/v1/auth/2fa/login",
            json={
                "partial_token": partial,
                "totp_code": _future_totp(secret),
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["scope"] == SCOPE_FULL

    def test_two_factor_login_with_invalid_totp_returns_401(
        self, client: TestClient
    ) -> None:
        # AC: invalid TOTP at /2fa/login → 401.
        phone = "13800138004"
        parent_id = _send_and_login(client, phone)
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        code = _send_sms_code(phone)
        r = client.post(
            "/api/v1/auth/sms/login",
            json={"phone": phone, "code": code},
        )
        partial = r.json()["partial_token"]
        r = client.post(
            "/api/v1/auth/2fa/login",
            json={"partial_token": partial, "totp_code": "000000"},
        )
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "TWO_FA_INVALID_CODE"

    def test_deletion_request_blocked_when_2fa_enabled_without_proof(
        self, client: TestClient
    ) -> None:
        # AC-6: deletion request with 2FA enabled but no proof → 401.
        phone = "13800138005"
        parent_id = _send_and_login(client, phone)
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        # No X-2FA-Code / X-2FA-Backup-Code header; the gate
        # denies because the token's scope is `full` (not
        # `full_reauth`).
        r = client.post(
            "/api/v1/auth/deletion/request",
            json={"parent_id": parent_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "TWO_FA_REQUIRED"

    def test_deletion_request_with_totp_header_succeeds(
        self, client: TestClient
    ) -> None:
        # AC-6: X-2FA-Code header admits the caller.
        phone = "13800138006"
        parent_id = _send_and_login(client, phone)
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        # Use the service singleton directly so we don't burn
        # the current TOTP step on enrollment (which would
        # otherwise make the X-2FA-Code header below look like
        # a replay). begin_enrollment + verify_enrollment
        # together store the secret + codes but the TOTP step
        # at verify_enrollment time IS the same step the header
        # would carry, so the simpler path is to bypass it and
        # synthesize an enabled row straight from the service.
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        # Pre-seed: write the enabled row directly so the
        # header test can use a fresh current code.
        from niki.db.repositories import get_two_factor_repository
        from niki.db.repositories.two_factor_repo import (
            TwoFactorConfigData,
        )
        from datetime import datetime, timezone
        repo = get_two_factor_repository()
        from niki.services.two_factor import (
            InMemoryTwoFactorService,
            get_two_factor_service,
        )
        svc = get_two_factor_service()  # type: ignore[assignment]
        # Encrypt secret with the live Fernet key (the test
        # fixture installed a known key; we reuse it here).
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        # Use the service's actual key (which is a Fernet key
        # established by the autouse fixture).
        live_key = svc._fernet_key  # type: ignore[attr-defined]
        ciphertext = Fernet(live_key).encrypt(
            secret.encode("ascii")
        ).decode("ascii")
        await_run(
            repo.upsert(
                parent_id=parent_id,
                secret_encrypted=ciphertext,
                is_enabled=True,
                enabled_at=datetime.now(timezone.utc),
                last_used_at=None,
                backup_codes_hash=None,
                backup_codes_used=[],
            )
        )
        # Fresh current-step code for the deletion header —
        # not consumed yet.
        new_code = pyotp.TOTP(secret).now()
        r = client.post(
            "/api/v1/auth/deletion/request",
            json={"parent_id": parent_id},
            headers={
                "Authorization": f"Bearer {token}",
                "X-2FA-Code": new_code,
            },
        )
        assert r.status_code == 200, r.text
        assert (
            r.json()["account"]["status"] == "deletion_pending"
        )

    def test_deletion_request_with_backup_code_header_succeeds(
        self, client: TestClient
    ) -> None:
        # AC-6: X-2FA-Backup-Code header admits the caller.
        phone = "13800138007"
        parent_id = _send_and_login(client, phone)
        token = issue_access_token(parent_id, scope=SCOPE_FULL)
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        codes = client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["backup_codes"]
        # Backup codes are independent of TOTP windows, so no
        # timing gymnastics are needed.
        r = client.post(
            "/api/v1/auth/deletion/request",
            json={"parent_id": parent_id},
            headers={
                "Authorization": f"Bearer {token}",
                "X-2FA-Backup-Code": codes[0],
            },
        )
        assert r.status_code == 200, r.text
        assert (
            r.json()["account"]["status"] == "deletion_pending"
        )

    def test_audit_log_records_2fa_events(
        self, client: TestClient, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        # AC-7: enrollment writes audit rows.
        token = issue_access_token(
            "p_audit_test", scope=SCOPE_FULL
        )
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        rows = await_run(
            audit_repo.list_events(
                limit=20,
                actor_parent_id="p_audit_test",
            )
        )
        event_types = {row["event_type"] for row in rows}
        assert "2fa_enrollment_started" in event_types
        assert "2fa_enrollment_completed" in event_types

    def test_verify_endpoint_failure_writes_audit_row(
        self, client: TestClient, audit_repo: InMemoryAuditLogRepository
    ) -> None:
        # AC-7: failed verification writes a warning row.
        token = issue_access_token(
            "p_audit_fail", scope=SCOPE_FULL
        )
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Wrong code on /verify.
        r = client.post(
            "/api/v1/auth/2fa/verify",
            json={"totp_code": "000000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401
        rows = await_run(
            audit_repo.list_events(
                limit=20,
                actor_parent_id="p_audit_fail",
            )
        )
        event_types = {row["event_type"] for row in rows}
        assert "2fa_verification_failure" in event_types
        failure = [
            r for r in rows
            if r["event_type"] == "2fa_verification_failure"
        ][0]
        assert failure["severity"] == "warning"

    def test_rate_limit_on_verify_endpoint(
        self, client: TestClient
    ) -> None:
        # AC-8: 6th verify call within 60s → 429.
        token = issue_access_token(
            "p_rate_test", scope=SCOPE_FULL
        )
        r = client.post(
            "/api/v1/auth/2fa/enroll",
            headers={"Authorization": f"Bearer {token}"},
        )
        secret = r.json()["secret"]
        client.post(
            "/api/v1/auth/2fa/enroll/verify",
            json={"totp_code": pyotp.TOTP(secret).now()},
            headers={"Authorization": f"Bearer {token}"},
        )
        # 5 wrong attempts within the window.
        for _ in range(5):
            r = client.post(
                "/api/v1/auth/2fa/verify",
                json={"totp_code": "000000"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 401, r.text
        # 6th → 429.
        r = client.post(
            "/api/v1/auth/2fa/verify",
            json={"totp_code": "000000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 429, r.text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def await_run(coro):
    """Run an async coroutine synchronously in tests."""
    import asyncio
    return asyncio.run(coro)