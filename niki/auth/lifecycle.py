"""Account lifecycle — PRD #30, #35, #70.

Orchestrates the multi-step signup and the multi-step deletion:
- Signup: pending → real-name → face → consent → verified
- Deletion: verified → deletion_pending → (30 days) → deleted

PRD #35: all data must be erased within 30 days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import structlog

from niki.auth.account import (
    AccountStatus,
    AuthError,
    ParentAccount,
    mask_id_number,
    mask_phone,
    validate_id_number,
    validate_phone,
)
from niki.auth.consent import ConsentManager, ConsentType
from niki.auth.face_verify import FaceVerifier
from niki.auth.real_name import RealNameVerifier

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
                s.completed_at = datetime.utcnow() if completed else None
                return


class AccountLifecycle:
    """Orchestrates the parent signup + deletion lifecycle.

    Usage:
        lifecycle = AccountLifecycle()
        account, progress = await lifecycle.start_signup(name, id, phone)
        await lifecycle.verify_real_name(...)
        await lifecycle.verify_face(...)
        await lifecycle.sign_consent(...)
        # account.status == VERIFIED
    """

    def __init__(self) -> None:
        self._real_name = RealNameVerifier()
        self._face = FaceVerifier()
        self._consent = ConsentManager()
        self._accounts: dict[str, ParentAccount] = {}
        self._progress: dict[str, SignupProgress] = {}

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
        account = ParentAccount(
            id=parent_id,
            name=name,
            id_number_masked=mask_id_number(id_number),
            phone_masked=mask_phone(phone),
            email=email,
            status=AccountStatus.PENDING,
        )
        self._accounts[parent_id] = account

        progress = SignupProgress(
            parent_id=parent_id,
            steps=[
                SignupStep("real_name"),
                SignupStep("face"),
                SignupStep("consent"),
            ],
        )
        self._progress[parent_id] = progress

        logger.info(
            "auth.signup.started",
            parent_id=parent_id,
            name=name,
            id_masked=account.id_number_masked,
        )
        return account, progress

    async def verify_real_name(
        self,
        parent_id: str,
        name: str,
        id_number: str,
    ) -> ParentAccount:
        """Step 1: Real-name verification."""
        self._check_account(parent_id)
        progress = self._progress[parent_id]

        try:
            result = await self._real_name.verify(name, id_number)
        except AuthError as exc:
            progress.mark_step("real_name", completed=False, error=exc.message)
            raise

        if not result.verified:
            progress.mark_step("real_name", completed=False, error=result.error)
            raise AuthError(
                code="REAL_NAME_FAILED",
                message=result.error or "Real-name verification failed",
            )

        progress.mark_step("real_name", completed=True)
        return self._accounts[parent_id]

    async def verify_face(
        self,
        parent_id: str,
        live_image_url: str,
        reference_image_url: str | None = None,
    ) -> ParentAccount:
        """Step 2: Face verification."""
        self._check_account(parent_id)
        progress = self._progress[parent_id]

        try:
            result = await self._face.verify(live_image_url, reference_image_url)
        except AuthError as exc:
            progress.mark_step("face", completed=False, error=exc.message)
            raise

        if not result.verified:
            progress.mark_step("face", completed=False, error=result.error)
            raise AuthError(
                code="FACE_FAILED",
                message=result.error or "Face verification failed",
            )

        progress.mark_step("face", completed=True)
        return self._accounts[parent_id]

    async def sign_consent(
        self,
        parent_id: str,
        typed_signature: str,
        ip_address: str,
        device_fingerprint: str,
        sign_child_consent: bool = True,
    ) -> ParentAccount:
        """Step 3: Sign the consent document(s).

        The parent signs the parent version; the child version is signed
        on behalf of the child (typed by parent).

        Once all 3 steps complete, account moves to VERIFIED.
        """
        self._check_account(parent_id)
        progress = self._progress[parent_id]

        # Sign parent version
        self._consent.sign(
            parent_id=parent_id,
            consent_type=ConsentType.PARENT,
            typed_signature=typed_signature,
            ip_address=ip_address,
            device_fingerprint=device_fingerprint,
        )

        # Optionally sign child version too
        if sign_child_consent:
            self._consent.sign(
                parent_id=parent_id,
                consent_type=ConsentType.CHILD,
                typed_signature=typed_signature,
                ip_address=ip_address,
                device_fingerprint=device_fingerprint,
            )

        progress.mark_step("consent", completed=True)

        # Check if all 3 steps complete → mark VERIFIED
        if progress.all_completed:
            account = self._accounts[parent_id]
            account.status = AccountStatus.VERIFIED
            account.verified_at = datetime.utcnow()
            logger.info(
                "auth.signup.completed",
                parent_id=parent_id,
            )

        return self._accounts[parent_id]

    # ---- Deletion ----

    async def request_deletion(self, parent_id: str) -> ParentAccount:
        """Request account deletion (starts 30-day window, PRD #35)."""
        self._check_account(parent_id)
        account = self._accounts[parent_id]

        if account.status == AccountStatus.DELETED:
            raise AuthError("ALREADY_DELETED", "Account already deleted")
        if account.status == AccountStatus.DELETION_PENDING:
            raise AuthError("ALREADY_PENDING", "Deletion already in progress")

        account.status = AccountStatus.DELETION_PENDING
        account.deletion_requested_at = datetime.utcnow()

        logger.info(
            "auth.deletion.requested",
            parent_id=parent_id,
            window_remaining=account.deletion_window_remaining_days,
        )
        return account

    async def complete_deletion(self, parent_id: str) -> ParentAccount:
        """Complete the deletion (called by the 30-day cron job)."""
        self._check_account(parent_id)
        account = self._accounts[parent_id]

        if account.status != AccountStatus.DELETION_PENDING:
            raise AuthError(
                "DELETION_NOT_PENDING",
                f"Account not in DELETION_PENDING state (status={account.status})",
            )

        # Erase all associated data
        self._purge_account_data(parent_id)
        account.status = AccountStatus.DELETED
        account.deletion_completed_at = datetime.utcnow()
        account.child_ids = []
        account.metadata.clear()

        logger.info("auth.deletion.completed", parent_id=parent_id)
        return account

    def _purge_account_data(self, parent_id: str) -> None:
        """Purge all data associated with the parent.

        Production: would also erase:
        - Conversations (Postgres)
        - Voice recordings (edge-encrypted)
        - Vector embeddings (Chroma)
        - Cognitive graph nodes (Neo4j)
        - Emotion baseline (Redis)
        - Consent records (Crypto-shred)
        """
        # Remove account + progress
        self._accounts.pop(parent_id, None)
        self._progress.pop(parent_id, None)
        # Remove consent records (best-effort)
        # In production: would iterate self._consent._records

    # ---- Lookup ----

    def get_account(self, parent_id: str) -> ParentAccount | None:
        return self._accounts.get(parent_id)

    def get_progress(self, parent_id: str) -> SignupProgress | None:
        return self._progress.get(parent_id)

    def get_consent_manager(self) -> ConsentManager:
        return self._consent

    def list_accounts(self) -> list[ParentAccount]:
        return list(self._accounts.values())

    # ---- Helpers ----

    def _check_account(self, parent_id: str) -> None:
        account = self._accounts.get(parent_id)
        if account is None:
            raise AuthError("ACCOUNT_NOT_FOUND", f"Account {parent_id} not found")
        if account.status == AccountStatus.DELETED:
            raise AuthError("ACCOUNT_DELETED", "Account has been deleted")

    @staticmethod
    def _make_id(name: str, id_number: str) -> str:
        import hashlib
        return hashlib.sha256(f"{name}:{id_number}".encode()).hexdigest()[:16]
