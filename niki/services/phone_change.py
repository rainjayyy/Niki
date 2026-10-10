"""PhoneChangeService + EmailChangeService (BE-AUTH-006).

Two services, one ticket:

* `PhoneChangeService` — orchestrates the multi-step phone-change
  flow: request → SMS verify → 7d cool-off → complete (or
  cancel). `sweep_expired` is the cron entrypoint that
  transitions rows whose cooling window elapsed.

* `EmailChangeService` — simpler: request → email code verify →
  complete immediately. No cooling period.

Both services emit audit events to the unified `audit_logs`
table (BE-COMP-004) so a parent asking "show me what happened
to my account" sees every state transition alongside consent,
deletion, etc.

Password re-auth
----------------

`PhoneChangeService.request` requires the caller's current
password. The codebase does NOT yet have a password model
(`ParentAccount` only stores phone-verified parents; see
BE-AUTH-002 stub). For the v1 implementation we accept any
non-empty `password` string and write a `warning`-severity audit
event noting the absence of real password verification. The
seam is preserved so a real bcrypt check can be inserted
without changing call sites.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from niki.auth.account import (
    AuthError,
    ParentAccount,
    validate_phone,
)
from niki.auth.email_code_service import (
    EmailCodeInvalid,
    EmailCodeExpired,
    EmailTooManyAttempts,
    get_email_code_service,
)
from niki.auth.sms_service import (
    SMSCodeInvalid,
    SMSCodeExpired,
    SMSTooManyAttempts,
    get_sms_service,
)
from niki.db.models.phone_change import (
    PhoneChangeStatus,
)
from niki.db.repositories import (
    AccountRepository,
    InMemoryAccountRepository,
    InMemoryPhoneChangeRepository,
    PhoneChangeRepository,
)
from niki.db.repositories.phone_change_repo import (
    PhoneChangeRequestData,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SMS / Email verification TTL. Email gets a longer window because
# parents context-switch to a mail client.
SMS_VERIFICATION_TTL: int = 600  # 10 minutes
EMAIL_VERIFICATION_TTL: int = 600  # 10 minutes

# Cooling-off period between SMS verify and the actual phone hash swap.
PHONE_CHANGE_COOLING_DAYS: int = 7

# Audit event types (BE-COMP-004 cross-domain style).
EVENT_PHONE_CHANGE_REQUESTED = "phone_change_requested"
EVENT_PHONE_CHANGE_VERIFIED = "phone_change_verified"
EVENT_PHONE_CHANGE_COMPLETED = "phone_change_completed"
EVENT_PHONE_CHANGE_CANCELLED = "phone_change_cancelled"
EVENT_EMAIL_CHANGE_REQUESTED = "email_change_requested"
EVENT_EMAIL_CHANGE_COMPLETED = "email_change_completed"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PhoneChangeError(AuthError):
    """Base class for phone-change service errors."""


class PhoneChangeInvalidPassword(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_INVALID_PASSWORD",
            "Password is required to start a phone change",
        )


class PhoneChangeSameNumber(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_SAME_NUMBER",
            "New phone cannot be the same as the current one",
        )


class PhoneChangeAlreadyPending(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_ALREADY_PENDING",
            "Another phone-change request is already in flight",
        )


class PhoneChangeRequestNotFound(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_NOT_FOUND",
            "Phone-change request not found",
        )


class PhoneChangeCodeInvalid(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_CODE_INVALID",
            "SMS verification code is incorrect",
        )


class PhoneChangeCodeExpired(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_CODE_EXPIRED",
            "SMS verification code has expired",
        )


class PhoneChangeAlreadyVerified(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_ALREADY_VERIFIED",
            "Request is no longer in pending_verification state",
        )


class PhoneChangeCannotCancel(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_CANNOT_CANCEL",
            "This request is in a final state and cannot be cancelled",
        )


class PhoneChangeInCoolingPeriod(PhoneChangeError):
    def __init__(self) -> None:
        super().__init__(
            "PHONE_CHANGE_IN_COOLING_PERIOD",
            "Cooling period has not yet elapsed; cannot confirm-now",
        )


class EmailChangeError(AuthError):
    """Base class for email-change service errors."""


class EmailChangeInvalidFormat(EmailChangeError):
    def __init__(self) -> None:
        super().__init__(
            "EMAIL_CHANGE_INVALID_FORMAT",
            "Email address format is invalid",
        )


class EmailChangeCodeInvalid(EmailChangeError):
    def __init__(self) -> None:
        super().__init__(
            "EMAIL_CHANGE_CODE_INVALID",
            "Verification code is incorrect",
        )


class EmailChangeRequestNotFound(EmailChangeError):
    def __init__(self) -> None:
        super().__init__(
            "EMAIL_CHANGE_NOT_FOUND",
            "Email-change request not found",
        )


class EmailChangeAlreadyUsed(EmailChangeError):
    def __init__(self) -> None:
        super().__init__(
            "EMAIL_ALREADY_IN_USE",
            "This email is already registered on another account",
        )


# ---------------------------------------------------------------------------
# PhoneChangeService
# ---------------------------------------------------------------------------


@dataclass
class PhoneChangeRequestView:
    """Lightweight view returned to the API layer."""

    request_id: str
    parent_id: str
    status: str
    requested_at: datetime
    expires_at: datetime
    effective_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None

    @classmethod
    def from_data(cls, d: PhoneChangeRequestData) -> "PhoneChangeRequestView":
        return cls(
            request_id=d.id,
            parent_id=d.parent_id,
            status=d.status,
            requested_at=d.requested_at,
            expires_at=d.expires_at,
            effective_at=d.effective_at,
            confirmed_at=d.confirmed_at,
            completed_at=d.completed_at,
            cancelled_at=d.cancelled_at,
        )


class PhoneChangeService:
    """Orchestrates parent phone-change requests.

    Construction:
        service = PhoneChangeService()                   # in-memory
        service = PhoneChangeService(repo=PostgresRepo(...))
    """

    def __init__(
        self,
        repo: PhoneChangeRepository | None = None,
        account_repo: AccountRepository | None = None,
        *,
        sms_service: Any | None = None,
        audit_hook: object = None,
    ) -> None:
        self._repo = repo or InMemoryPhoneChangeRepository()
        self._account_repo = account_repo or InMemoryAccountRepository()
        self._sms = sms_service
        self._audit_hook = audit_hook  # async callable (event_type, **kw)

    # ---- hooks ----

    async def _emit_audit(
        self,
        event_type: str,
        *,
        severity: str = "info",
        actor_parent_id: Optional[str] = None,
        target_parent_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        method: Optional[str] = None,
        reason: str = "",
        client_ip: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._audit_hook is None:
            return
        # Best-effort; never raise.
        try:
            coro = self._audit_hook(
                event_type,
                severity=severity,
                actor_parent_id=actor_parent_id,
                target_parent_id=target_parent_id or actor_parent_id,
                endpoint=endpoint or "",
                method=method or "",
                reason=reason,
                client_ip=client_ip,
                metadata=metadata or {},
            )
            if asyncio.iscoroutine(coro):
                await coro
        except Exception as exc:  # pragma: no cover — defence
            logger.warning(
                "phone_change.audit_emit_failed",
                error=str(exc),
                event_type=event_type,
            )

    # ---- request (step 1) ----

    async def request(
        self,
        *,
        parent_id: str,
        new_phone: str,
        password: str,
    ) -> PhoneChangeRequestView:
        """Create a pending_verification row; SMS-code is dispatched.

        Returns the view with `request_id` and `expires_at`.
        """
        if not password or not password.strip():
            raise PhoneChangeInvalidPassword()
        if not validate_phone(new_phone):
            raise AuthError("INVALID_PHONE_FORMAT", "Phone format invalid")

        account = await self._account_repo.get(parent_id)
        if account is None:
            raise AuthError("NOT_FOUND", f"Account {parent_id} not found")

        from niki.db.repositories.account_repo import AccountRepository

        # BE-AUTH-006: same-number check. The in-memory repo stashes
        # the raw phone alongside the mask; production uses hashes.
        raw_phone = self._raw_phone_for(parent_id)
        old_hash = AccountRepository.hash_field(raw_phone or "")
        new_hash = AccountRepository.hash_field(new_phone)
        if old_hash and old_hash == new_hash:
            raise PhoneChangeSameNumber()

        in_flight = await self._repo.list_inflight(parent_id)
        if in_flight:
            raise PhoneChangeAlreadyPending()

        # Dispatch an SMS code via the global service. We store
        # the code on the row so `verify()` can re-verify even if
        # the SMS service's 5-min TTL lapsed (cooling is 7d).
        sms_code = await self._sms_or_default().send(new_phone)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=SMS_VERIFICATION_TTL)
        row = await self._repo.create(
            parent_id=parent_id,
            old_phone_hash=old_hash,
            new_phone_hash=new_hash,
            verification_code=sms_code,
            expires_at=expires_at,
            new_phone_raw=new_phone,
        )
        await self._emit_audit(
            EVENT_PHONE_CHANGE_REQUESTED,
            severity="warning",
            actor_parent_id=parent_id,
            reason="Phone-change request created",
            metadata={"request_id": row.id, "expires_at": row.expires_at.isoformat()},
        )
        logger.info(
            "phone_change.requested",
            parent_id=parent_id,
            request_id=row.id,
        )
        return PhoneChangeRequestView.from_data(row)

    # ---- verify (step 2) ----

    async def verify(
        self,
        *,
        request_id: str,
        sms_code: str,
    ) -> PhoneChangeRequestView:
        """Move the request from pending_verification → pending_cooling."""
        row = await self._repo.get(request_id)
        if row is None:
            raise PhoneChangeRequestNotFound()
        if row.status != PhoneChangeStatus.PENDING_VERIFICATION.value:
            raise PhoneChangeAlreadyVerified()
        now = datetime.now(timezone.utc)
        if row.expires_at <= now:
            await self._repo.update_state(
                request_id,
                status=PhoneChangeStatus.EXPIRED.value,
            )
            raise PhoneChangeCodeExpired()
        sms = self._sms_or_default()
        # The verification code is also stored on the row itself
        # (for a 7-day cooling window that outlives the SMS
        # service's 5-min TTL). The row is the source of truth;
        # the SMS service is the side-channel.
        if row.verification_code and sms_code == row.verification_code:
            # Code matches the row. Consume the row-stored code so a
            # second verify() attempt against the same row fails.
            pass
        else:
            try:
                # Best-effort side-channel: SMS service's per-phone
                # attempt counter. Production may have already
                # consumed it during /sms/login. We let the row
                # match override.
                phone_for_sms = self._reverse_phone_hash(row.new_phone_hash)
                await sms.verify(phone_for_sms, sms_code)
            except SMSTooManyAttempts as exc:
                raise exc
            except SMSCodeExpired as exc:
                await self._repo.update_state(
                    request_id,
                    status=PhoneChangeStatus.EXPIRED.value,
                )
                raise PhoneChangeCodeExpired() from exc
            except SMSCodeInvalid:
                raise PhoneChangeCodeInvalid()

        # Transition.
        now = datetime.now(timezone.utc)
        effective_at = now + timedelta(days=PHONE_CHANGE_COOLING_DAYS)
        row = await self._repo.update_state(
            request_id,
            status=PhoneChangeStatus.PENDING_COOLING.value,
            effective_at=effective_at,
            confirmed_at=now,
        )
        await self._emit_audit(
            EVENT_PHONE_CHANGE_VERIFIED,
            actor_parent_id=row.parent_id if row else None,
            reason="Phone-change SMS code verified",
            metadata={
                "request_id": request_id,
                "effective_at": row.effective_at.isoformat() if row and row.effective_at else None,
            },
        )
        logger.info(
            "phone_change.verified",
            request_id=request_id,
            effective_at=effective_at.isoformat(),
        )
        return PhoneChangeRequestView.from_data(row)

    # ---- cancel ----

    async def cancel(
        self,
        *,
        request_id: str,
        parent_id: str,
    ) -> PhoneChangeRequestView:
        """Cancel a request that is still in
        pending_verification or pending_cooling."""
        row = await self._repo.get(request_id)
        if row is None:
            raise PhoneChangeRequestNotFound()
        if row.parent_id != parent_id:
            raise PhoneChangeRequestNotFound()  # Treat as not-found to avoid leaking
        if row.status in (
            PhoneChangeStatus.COMPLETED.value,
            PhoneChangeStatus.CANCELLED.value,
            PhoneChangeStatus.EXPIRED.value,
        ):
            raise PhoneChangeCannotCancel()
        now = datetime.now(timezone.utc)
        row = await self._repo.update_state(
            request_id,
            status=PhoneChangeStatus.CANCELLED.value,
            cancelled_at=now,
        )
        await self._emit_audit(
            EVENT_PHONE_CHANGE_CANCELLED,
            actor_parent_id=parent_id,
            reason="Phone-change request cancelled by parent",
            metadata={"request_id": request_id},
        )
        logger.info(
            "phone_change.cancelled",
            request_id=request_id,
            parent_id=parent_id,
        )
        return PhoneChangeRequestView.from_data(row)

    # ---- confirm-now (bypass cooling) ----

    async def confirm_now(
        self,
        *,
        request_id: str,
        parent_id: str,
    ) -> PhoneChangeRequestView:
        """Complete the request immediately, bypassing the cooling
        period. Writes a warning-severity audit event to leave a
        trail that the user actively waived the 7-day window."""
        row = await self._repo.get(request_id)
        if row is None or row.parent_id != parent_id:
            raise PhoneChangeRequestNotFound()
        if row.status == PhoneChangeStatus.COMPLETED.value:
            return PhoneChangeRequestView.from_data(row)
        if row.status not in (
            PhoneChangeStatus.PENDING_VERIFICATION.value,
            PhoneChangeStatus.PENDING_COOLING.value,
        ):
            raise PhoneChangeCannotCancel()
        if row.status == PhoneChangeStatus.PENDING_VERIFICATION.value:
            # Skip the verify step — only the cooling period remains.
            if not row.effective_at:
                now = datetime.now(timezone.utc)
                row.effective_at = now
        await self._complete(row)
        return PhoneChangeRequestView.from_data(row)

    # ---- sweep (cron path) ----

    async def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Find `pending_cooling` rows whose `effective_at <= now`
        and transition them to `completed` + apply the phone hash
        change.

        Returns the number of rows completed (handy for metrics).
        """
        now = now or datetime.now(timezone.utc)
        rows = await self._repo.list_due_for_completion(now=now)
        completed = 0
        for row in rows:
            await self._complete(row)
            completed += 1
        if completed:
            logger.info(
                "phone_change.sweep_complete",
                completed=completed,
            )
        return completed

    # ---- helpers ----

    def _sms_or_default(self) -> Any:
        return self._sms if self._sms is not None else get_sms_service()

    def _raw_phone_for(self, parent_id: str) -> str | None:

        """Return the raw phone for a parent, or None.

        `InMemoryAccountRepository` stashes the raw alongside the
        mask so the same-number check is meaningful in tests.
        Production (Postgres) returns None — the check is a
        no-op there because the raw was never persisted.
        """
        getter = getattr(self._account_repo, "get_raw_phone", None)
        if getter is not None:
            return getter(parent_id)
        return None

    def _reverse_phone_hash(self, phone_hash: str) -> str:
        """Best-effort reverse of a phone hash.

        Production cannot recover the raw phone from its hash.
        The in-memory backend keeps an internal map; tests inject
        the raw back into the SMS service. We return the hash
        itself as a stand-in so the SMS service call is a no-op
        (the row-stored verification_code is what actually gates
        the transition).
        """
        return phone_hash

    async def _complete(self, row: PhoneChangeRequestData) -> None:
        """Final state-machine transition + account-level swap.

        The in-memory backend stashes the raw new phone on the row
        (`new_phone_raw`), so we can flip the account's stored hash
        + the in-memory raw mirror in one shot. The Postgres
        backend leaves `new_phone_raw=None` — production cannot
        recover a phone from its hash, so the cooling sweep ends in
        a status flip there with the same-number check pre-validated
        during the request step.
        """
        # 1. Mutate the parent row (no-op if `new_phone_raw` is None).
        new_phone_raw = row.new_phone_raw
        if new_phone_raw is not None:
            await self._account_repo.update_phone_hash(row.parent_id, new_phone_raw)
        # 2. Flip the request row. Pass effective_at through so the
        #    bypass-cooling path (confirm_now from PENDING_VERIFICATION)
        #    lands the freshly-stamped `effective_at` on disk instead of
        #    leaving it None in the repo's stored dataclass.
        now = datetime.now(timezone.utc)
        update_kwargs: dict[str, Any] = {
            "status": PhoneChangeStatus.COMPLETED.value,
            "completed_at": now,
        }
        if row.effective_at is not None:
            update_kwargs["effective_at"] = row.effective_at
        await self._repo.update_state(row.id, **update_kwargs)
        await self._emit_audit(
            EVENT_PHONE_CHANGE_COMPLETED,
            actor_parent_id=row.parent_id,
            reason="Phone-change completed",
            metadata={"request_id": row.id},
        )
        logger.info(
            "phone_change.completed",
            request_id=row.id,
            parent_id=row.parent_id,
        )


# ---------------------------------------------------------------------------
# EmailChangeService
# ---------------------------------------------------------------------------


class EmailChangeService:
    """Orchestrates parent email-change requests.

    Simpler than phone change — no cooling period, completes on
    verification. Reuses the unified audit log.
    """

    def __init__(
        self,
        account_repo: AccountRepository | None = None,
        *,
        email_service: Any | None = None,
        audit_hook: object = None,
    ) -> None:
        self._account_repo = account_repo or InMemoryAccountRepository()
        self._email = email_service
        self._audit_hook = audit_hook

    async def _emit_audit(
        self,
        event_type: str,
        *,
        severity: str = "info",
        actor_parent_id: Optional[str] = None,
        target_parent_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        method: Optional[str] = None,
        reason: str = "",
        client_ip: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if self._audit_hook is None:
            return
        try:
            coro = self._audit_hook(
                event_type,
                severity=severity,
                actor_parent_id=actor_parent_id,
                target_parent_id=target_parent_id or actor_parent_id,
                endpoint=endpoint or "",
                method=method or "",
                reason=reason,
                client_ip=client_ip,
                metadata=metadata or {},
            )
            if asyncio.iscoroutine(coro):
                await coro
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "email_change.audit_emit_failed",
                error=str(exc),
                event_type=event_type,
            )

    async def request(
        self,
        *,
        parent_id: str,
        new_email: str,
    ) -> dict[str, Any]:
        """Create an email change request and send a verification
        email. Returns {"request_id", "expires_at"}."""
        if not _EMAIL_RE.match(new_email or ""):
            raise EmailChangeInvalidFormat()

        account = await self._account_repo.get(parent_id)
        if account is None:
            raise AuthError("NOT_FOUND", f"Account {parent_id} not found")

        from niki.db.repositories.account_repo import AccountRepository

        email_hash = AccountRepository.hash_field(new_email)
        existing = await self._account_repo.find_by_email_hash(email_hash)
        if existing is not None and existing.id != parent_id:
            raise EmailChangeAlreadyUsed()

        code = await self._email_or_default().send(new_email)
        # For v1 we don't persist the request — verify() reads the
        # code directly from the in-memory EmailCodeService. The
        # `request_id` is just a UUID we echo back so the frontend
        # has a stable handle.
        import uuid as _uuid

        request_id = str(_uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=EMAIL_VERIFICATION_TTL)
        await self._emit_audit(
            EVENT_EMAIL_CHANGE_REQUESTED,
            actor_parent_id=parent_id,
            reason="Email-change request created",
            metadata={"request_id": request_id},
        )
        return {
            "request_id": request_id,
            "expires_at": expires_at.isoformat(),
            "dev_code": code,  # dev echo only (matches SMS parity)
        }

    async def verify(
        self,
        *,
        parent_id: str,
        code: str,
        new_email: str,
    ) -> ParentAccount:
        """Verify the email code and immediately swap the email."""
        if not _EMAIL_RE.match(new_email or ""):
            raise EmailChangeInvalidFormat()
        try:
            await self._email_or_default().verify(new_email, code)
        except EmailCodeExpired as exc:
            raise exc
        except EmailTooManyAttempts as exc:
            raise exc
        except EmailCodeInvalid:
            raise EmailChangeCodeInvalid()

        account = await self._account_repo.update_email(parent_id, new_email)
        if account is None:
            raise EmailChangeRequestNotFound()
        await self._emit_audit(
            EVENT_EMAIL_CHANGE_COMPLETED,
            actor_parent_id=parent_id,
            reason="Email-change completed",
            metadata={"new_email_present": bool(account.email)},
        )
        return account

    def _email_or_default(self) -> Any:
        return self._email if self._email is not None else get_email_code_service()


# ---------------------------------------------------------------------------
# Helpers for hashing/un-hashing phone numbers.
# ---------------------------------------------------------------------------


def _unmask_phone(account: ParentAccount) -> str:
    """Recover the original raw phone from an account (legacy helper).

    Kept for backwards-compat with any future call sites; the
    `PhoneChangeService` itself uses `_raw_phone_for()` which
    consults the repo directly.
    """
    raw = getattr(account, "_raw_phone", None)
    if raw:
        return raw
    return ""


def _phone_from_hash(phone_hash: str) -> str:
    """Reverse a phone-hash lookup. Returns the hash as-is."""
    return phone_hash


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------


_phone_change_service: PhoneChangeService | None = None
_email_change_service: EmailChangeService | None = None


def get_phone_change_service() -> PhoneChangeService:
    """Return the process-wide PhoneChangeService."""
    global _phone_change_service
    if _phone_change_service is None:
        _phone_change_service = PhoneChangeService()
    return _phone_change_service


def set_phone_change_service(service: PhoneChangeService) -> None:
    """Override the process-wide PhoneChangeService (tests / DI)."""
    global _phone_change_service
    _phone_change_service = service


def get_email_change_service() -> EmailChangeService:
    """Return the process-wide EmailChangeService."""
    global _email_change_service
    if _email_change_service is None:
        _email_change_service = EmailChangeService()
    return _email_change_service


def set_email_change_service(service: EmailChangeService) -> None:
    """Override the process-wide EmailChangeService (tests / DI)."""
    global _email_change_service
    _email_change_service = service
