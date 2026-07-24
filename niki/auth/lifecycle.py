"""Account lifecycle - PRD #30, #35, #70.

Orchestrates the multi-step signup and the multi-step deletion:
- Signup: pending -> real-name -> face -> consent -> verified
- Deletion: verified -> deletion_pending -> (30 days) -> deleted

PRD #35: all data must be erased within 30 days.

**BE-STOR-001**: persistence moved from in-memory dicts to a
pluggable `AccountRepository` (`niki.db.repositories`). The default
constructor still uses `InMemoryAccountRepository` so legacy tests
pass unchanged, but production wiring should call
`AccountLifecycle(repo=PostgresAccountRepository(session))`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

import structlog

from niki.auth.account import (
    AccountStatus,
    AuthError,
    ParentAccount,
    validate_id_number,
    validate_phone,
)
from niki.auth.consent import ConsentManager, ConsentType
from niki.auth.face_verify import FaceVerifier
from niki.auth.real_name import RealNameVerifier
from niki.db.repositories import (
    AccountRepository,
    InMemoryAccountRepository,
)

logger = structlog.get_logger(__name__)


class DeletionStatus(StrEnum):
    """Status of a deletion request."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class SignupStep:
    """A single step in the signup pipeline."""

    name: str
    completed: bool = False
    completed_at: datetime | None = None
    error: str | None = None


@dataclass
class SignupProgress:
    """Tracks the multi-step signup."""

    parent_id: str
    steps: list[SignupStep] = field(default_factory=list)

    @property
    def all_completed(self) -> bool:
        return all(s.completed for s in self.steps)

    def mark_step(self, name: str, completed: bool, error: str | None = None) -> None:
        for s in self.steps:
            if s.name == name:
                s.completed = completed
                s.error = error
                s.completed_at = datetime.now(timezone.utc) if completed else None
                return


class AccountLifecycle:
    """Orchestrates the parent signup + deletion lifecycle.

    Construction:
        lifecycle = AccountLifecycle()                           # in-memory
        lifecycle = AccountLifecycle(repo=PostgresAccountRepo)   # production
    """

    def __init__(self, repo: AccountRepository | None = None) -> None:
        self._real_name = RealNameVerifier()
        self._face = FaceVerifier()
        self._consent = ConsentManager()
        self._repo = repo or InMemoryAccountRepository()

    # ---- Signup ----

    async def start_signup(
        self,
        name: str,
        id_number: str,
        phone: str,
        email: str | None = None,
    ) -> tuple[ParentAccount, SignupProgress]:
        """Begin the signup process. Creates a PENDING account.

        Raises:
            AuthError: If inputs are malformed.
        """
        if not validate_id_number(id_number):
            raise AuthError(
                code="INVALID_ID_FORMAT",
                message="ID number format invalid",
            )
        if not validate_phone(phone):
            raise AuthError(
                code="INVALID_PHONE_FORMAT",
                message="Phone format invalid",
            )

        parent_id = self._make_id(name, id_number)
        account = await self._repo.create(
            parent_id=parent_id,
            name=name,
            id_number=id_number,
            phone=phone,
            email=email,
        )
        progress = await self._repo.get_progress(parent_id)
        if progress is None:
            raise AuthError(
                "INTERNAL_STATE_ERROR",
                f"Progress not initialised after create for {parent_id}",
            )

        logger.info(
            "auth.signup.started",
            parent_id=parent_id,
            name=name,
            phone_masked=account.phone_masked,
        )
        return account, progress

    async def verify_real_name(
        self,
        parent_id: str,
        name: str,
        id_number: str,
    ) -> ParentAccount:
        """Step 1: Real-name verification."""
        await self._check_account(parent_id)

        try:
            result = await self._real_name.verify(name, id_number)
        except AuthError as exc:
            await self._repo.mark_verification(
                parent_id, "real_name", completed=False, error=exc.message
            )
            raise

        if not result.verified:
            await self._repo.mark_verification(
                parent_id,
                "real_name",
                completed=False,
                error=result.error or "Real-name verification failed",
            )
            raise AuthError(
                code="REAL_NAME_FAILED",
                message=result.error or "Real-name verification failed",
            )

        await self._repo.mark_verification(parent_id, "real_name", completed=True)
        logger.info("auth.signup.real_name_ok", parent_id=parent_id)
        return await self._get_or_raise(parent_id)

    async def verify_face(
        self,
        parent_id: str,
        live_image_url: str,
        reference_image_url: str | None = None,
    ) -> ParentAccount:
        """Step 2: Face verification."""
        await self._check_account(parent_id)

        try:
            result = await self._face.verify(live_image_url, reference_image_url)
        except AuthError as exc:
            await self._repo.mark_verification(
                parent_id, "face", completed=False, error=exc.message
            )
            raise

        if not result.verified:
            await self._repo.mark_verification(
                parent_id,
                "face",
                completed=False,
                error=result.error or "Face verification failed",
            )
            raise AuthError(
                code="FACE_FAILED",
                message=result.error or "Face verification failed",
            )

        await self._repo.mark_verification(parent_id, "face", completed=True)
        logger.info("auth.signup.face_ok", parent_id=parent_id)
        return await self._get_or_raise(parent_id)

    async def sign_consent(
        self,
        parent_id: str,
        typed_signature: str,
        ip_address: str,
        device_fingerprint: str,
        sign_child_consent: bool = True,
    ) -> ParentAccount:
        """Step 3: Sign the consent document(s)."""
        await self._check_account(parent_id)

        self._consent.sign(
            parent_id=parent_id,
            consent_type=ConsentType.PARENT,
            typed_signature=typed_signature,
            ip_address=ip_address,
            device_fingerprint=device_fingerprint,
        )
        if sign_child_consent:
            self._consent.sign(
                parent_id=parent_id,
                consent_type=ConsentType.CHILD,
                typed_signature=typed_signature,
                ip_address=ip_address,
                device_fingerprint=device_fingerprint,
            )

        await self._repo.mark_verification(parent_id, "consent", completed=True)

        progress = await self._repo.get_progress(parent_id)
        if progress and progress.all_completed:
            await self._repo.set_verified(parent_id)
            logger.info(
                "auth.signup.completed",
                parent_id=parent_id,
            )

        return await self._get_or_raise(parent_id)

    # ---- Deletion ----

    async def request_deletion(self, parent_id: str) -> ParentAccount:
        """Request account deletion (starts 30-day window, PRD #35)."""
        account = await self._check_account(parent_id)

        if account.status == AccountStatus.DELETED:
            raise AuthError("ALREADY_DELETED", "Account already deleted")
        if account.status == AccountStatus.DELETION_PENDING:
            raise AuthError("ALREADY_PENDING", "Deletion already in progress")

        await self._repo.set_deletion_pending(parent_id)

        logger.info(
            "auth.deletion.requested",
            parent_id=parent_id,
        )
        return await self._get_or_raise(parent_id)

    async def complete_deletion(self, parent_id: str) -> ParentAccount:
        """Complete the deletion (called by the 30-day cron job)."""
        await self._check_account(parent_id)

        account = await self._repo.get(parent_id)
        if account is None or account.status != AccountStatus.DELETION_PENDING:
            raise AuthError(
                "DELETION_NOT_PENDING",
                f"Account not in DELETION_PENDING state (status={account.status if account else 'missing'})",
            )

        # ISS-04 invariant: erase consent records (crypto-shred semantics).
        erased = self._consent.purge_for_parent(parent_id)
        logger.info(
            "auth.deletion.consent_purged",
            parent_id=parent_id,
            records_erased=erased,
        )

        # Hard-delete the parent row (FK CASCADE clears account_verifications).
        await self._repo.delete(parent_id)

        logger.info("auth.deletion.completed", parent_id=parent_id)
        # Sentinel empty account so the API still returns the correct shape.
        return ParentAccount(
            id=parent_id,
            name="",
            id_number_masked="****",
            phone_masked="****",
            status=AccountStatus.DELETED,
            deletion_completed_at=datetime.now(timezone.utc),
        )

    # ---- Lookup ----

    async def get_account(self, parent_id: str) -> ParentAccount | None:
        return await self._repo.get(parent_id)

    async def get_progress(self, parent_id: str) -> SignupProgress | None:
        return await self._repo.get_progress(parent_id)

    async def list_accounts(self) -> list[ParentAccount]:
        return await self._repo.list_all()

    def get_consent_manager(self) -> ConsentManager:
        return self._consent

    # ---- Helpers ----

    async def _check_account(self, parent_id: str) -> ParentAccount:
        account = await self._repo.get(parent_id)
        if account is None:
            raise AuthError("ACCOUNT_NOT_FOUND", f"Account {parent_id} not found")
        if account.status == AccountStatus.DELETED:
            raise AuthError("ACCOUNT_DELETED", "Account has been deleted")
        return account

    async def _get_or_raise(self, parent_id: str) -> ParentAccount:
        account = await self._repo.get(parent_id)
        if account is None:
            raise AuthError(
                "INTERNAL_STATE_ERROR",
                f"Account {parent_id} disappeared mid-flow",
            )
        return account

    @staticmethod
    def _make_id(name: str, id_number: str) -> str:
        import hashlib

        return hashlib.sha256(f"{name}:{id_number}".encode()).hexdigest()[:16]
