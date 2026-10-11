"""AccountRepository — persistence facade for parent accounts (BE-STOR-001).

Two implementations:

* `PostgresAccountRepository` — production. Uses SQLAlchemy AsyncSession,
  persists to PostgreSQL via the `parents` + `account_verifications` tables.

* `InMemoryAccountRepository` — tests + dev fallback. Mirrors the legacy
  in-dict behaviour so legacy unit tests stay green.

Both implementations expose the same interface (`AccountRepository` ABC)
and serialize via `to_business()` so callers (AccountLifecycle) work
against the dataclass `ParentAccount`, not the ORM.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from niki.auth.account import (
    AccountStatus,
    ParentAccount,
    mask_id_number,
    mask_phone,
)
from niki.db.models import (
    AccountVerification,
    Parent,
    ParentStatus,
)
from niki.db.models.account_verification import (
    ALL_STEPS,
    STEP_CONSENT,
    STEP_FACE,
    STEP_REAL_NAME,
)

if TYPE_CHECKING:
    from niki.auth.lifecycle import SignupProgress

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AccountRepository(ABC):
    """Persistence facade for parent accounts."""

    @abstractmethod
    async def get(self, parent_id: str) -> ParentAccount | None:
        """Look up a single parent by id."""

    @abstractmethod
    async def create(
        self,
        *,
        parent_id: str,
        name: str,
        id_number: str,
        phone: str,
        email: str | None = None,
    ) -> ParentAccount:
        """Persist a brand-new PENDING account + 3-step progress."""

    @abstractmethod
    async def get_progress(self, parent_id: str) -> "SignupProgress | None":
        """Return the 3-step progress (verifications) for a parent."""

    @abstractmethod
    async def mark_verification(
        self,
        parent_id: str,
        step: str,
        *,
        completed: bool,
        error: str | None = None,
    ) -> None:
        """Mark one signup step completed or failed."""

    @abstractmethod
    async def set_verified(self, parent_id: str) -> None:
        """Mark the parent account VERIFIED (all 3 steps done)."""

    @abstractmethod
    async def set_deletion_pending(self, parent_id: str) -> None:
        """Move the parent into the 30-day deletion window."""

    @abstractmethod
    async def set_deleted(self, parent_id: str) -> None:
        """Mark the parent fully deleted (after the cool-off window)."""

    @abstractmethod
    async def delete(self, parent_id: str) -> None:
        """Hard-delete the parent row (used after consent purge)."""

    @abstractmethod
    async def list_all(self) -> list[ParentAccount]:
        """All accounts (admin / debug only)."""

    @abstractmethod
    async def get_by_phone_hash(self, phone_hash: str) -> ParentAccount | None:
        """Look up an account by its sha256-hashed phone number.

        Used by PhoneChangeService to detect duplicate-phone collisions
        before granting a number-change request. Returns the existing
        account that owns \`phone_hash\`, or None if no match.
        """

    # ---- BE-AUTH-006 phone / email change plumbing ----

    @abstractmethod
    async def update_phone_hash(self, parent_id: str, new_phone: str) -> ParentAccount | None:
        """Swap the account's phone hash (called by PhoneChangeService
        once the cooling period elapses or after `confirm-now`)."""

    @abstractmethod
    async def update_email(self, parent_id: str, new_email: str) -> ParentAccount | None:
        """Swap the account's email + email_hash + flip verified=True."""

    @abstractmethod
    async def find_by_email_hash(self, email_hash: str) -> ParentAccount | None:
        """Look up an account by email hash (email uniqueness check)."""

    # ---- helpers ----

    @staticmethod
    def hash_field(value: str) -> str:
        """SHA-256 hex digest — used for id_number / phone de-duplication
        without storing PII."""
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Postgres implementation
# ---------------------------------------------------------------------------


class PostgresAccountRepository(AccountRepository):
    """AccountRepository backed by PostgreSQL (via SQLAlchemy async)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- queries ----

    async def get(self, parent_id: str) -> ParentAccount | None:
        from sqlalchemy.orm import selectinload

        result = await self._session.execute(
            select(Parent).where(Parent.id == parent_id).options(selectinload(Parent.children))
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return self._to_business(row)

    async def get_by_id_number_hash(self, id_number_hash: str) -> ParentAccount | None:
        result = await self._session.execute(
            select(Parent).where(Parent.id_number_hash == id_number_hash)
        )
        row = result.scalar_one_or_none()
        return self._to_business(row) if row else None

    async def get_by_phone_hash(self, phone_hash: str) -> ParentAccount | None:
        result = await self._session.execute(select(Parent).where(Parent.phone_hash == phone_hash))
        row = result.scalar_one_or_none()
        return self._to_business(row) if row else None

    async def list_all(self) -> list[ParentAccount]:
        from sqlalchemy.orm import selectinload

        result = await self._session.execute(select(Parent).options(selectinload(Parent.children)))
        rows = result.scalars().all()
        return [self._to_business(r) for r in rows]

    async def find_by_email_hash(self, email_hash: str) -> ParentAccount | None:
        """BE-AUTH-006: lookup by email hash (uniqueness check)."""
        result = await self._session.execute(select(Parent).where(Parent.email_hash == email_hash))
        row = result.scalar_one_or_none()
        return self._to_business(row) if row else None

    async def update_phone_hash(self, parent_id: str, new_phone: str) -> ParentAccount | None:
        """Swap the account's phone hash + masked view in place."""
        result = await self._session.execute(select(Parent).where(Parent.id == parent_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.phone_hash = self.hash_field(new_phone)
        await self._session.flush()
        return self._to_business(row)

    async def update_email(self, parent_id: str, new_email: str) -> ParentAccount | None:
        result = await self._session.execute(select(Parent).where(Parent.id == parent_id))
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.email = new_email
        row.email_hash = self.hash_field(new_email)
        row.email_verified = True
        await self._session.flush()
        return self._to_business(row)

    # ---- writes ----

    async def create(
        self,
        *,
        parent_id: str,
        name: str,
        id_number: str,
        phone: str,
        email: str | None = None,
    ) -> ParentAccount:
        from niki.auth.account import mask_id_number, mask_phone

        parent = Parent(
            id=parent_id,
            name=name,
            id_number_hash=self.hash_field(id_number),
            phone_hash=self.hash_field(phone),
            email=email,
            email_hash=(self.hash_field(email) if email else None),
            email_verified=False,
            status=ParentStatus.PENDING,
        )
        self._session.add(parent)
        # Eager-create 3 verification rows
        for step_name in ALL_STEPS:
            self._session.add(
                AccountVerification(
                    parent_id=parent_id,
                    step_name=step_name,
                    completed=False,
                )
            )
        await self._session.flush()  # populate server defaults
        logger.info(
            "account_repo.created",
            parent_id=parent_id,
            name=name,
        )
        # Build the business object from input args, NOT from ORM relationship
        # access — `parent.children` is empty by construction and would
        # trigger a lazy-load under the async greenlet.
        return ParentAccount(
            id=parent_id,
            name=name,
            id_number_masked=mask_id_number(id_number),
            phone_masked=mask_phone(phone),
            email=email,
            status=AccountStatus.PENDING,
            created_at=parent.created_at.replace(tzinfo=timezone.utc)
            if parent.created_at and parent.created_at.tzinfo is None
            else parent.created_at,
            child_ids=[],
            metadata={},
        )

    async def get_progress(self, parent_id: str) -> "SignupProgress | None":
        # Imported lazily to avoid circular dep on lifecycle module.
        from niki.auth.lifecycle import SignupProgress, SignupStep

        result = await self._session.execute(
            select(AccountVerification)
            .where(AccountVerification.parent_id == parent_id)
            .order_by(AccountVerification.id)  # ensures deterministic step order
        )
        rows = result.scalars().all()
        if not rows:
            return None
        steps = [
            SignupStep(
                name=r.step_name,
                completed=r.completed,
                completed_at=r.completed_at,
                error=r.error,
            )
            for r in rows
        ]
        return SignupProgress(parent_id=parent_id, steps=steps)

    async def mark_verification(
        self,
        parent_id: str,
        step: str,
        *,
        completed: bool,
        error: str | None = None,
    ) -> None:
        result = await self._session.execute(
            select(AccountVerification).where(
                AccountVerification.parent_id == parent_id,
                AccountVerification.step_name == step,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            logger.warning(
                "account_repo.verification_missing",
                parent_id=parent_id,
                step=step,
            )
            return
        row.completed = completed
        row.completed_at = datetime.now(timezone.utc) if completed else None
        row.error = error

    async def set_verified(self, parent_id: str) -> None:
        result = await self._session.execute(select(Parent).where(Parent.id == parent_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.status = ParentStatus.VERIFIED
        row.verified_at = datetime.now(timezone.utc)

    async def set_deletion_pending(self, parent_id: str) -> None:
        result = await self._session.execute(select(Parent).where(Parent.id == parent_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.status = ParentStatus.DELETION_PENDING
        row.deletion_requested_at = datetime.now(timezone.utc)

    async def set_deleted(self, parent_id: str) -> None:
        result = await self._session.execute(select(Parent).where(Parent.id == parent_id))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.status = ParentStatus.DELETED
        row.deletion_completed_at = datetime.now(timezone.utc)

    async def delete(self, parent_id: str) -> None:
        # SQLite (used in unit tests) does not honour FK CASCADE; the
        # PostgreSQL deployment does via ON DELETE CASCADE.  We delete
        # verifications explicitly to keep both backends consistent.
        await self._session.execute(
            delete(AccountVerification).where(AccountVerification.parent_id == parent_id)
        )
        await self._session.execute(delete(Parent).where(Parent.id == parent_id))

    # ---- mapping ----

    @staticmethod
    def _to_business(row: Parent) -> ParentAccount:
        return ParentAccount(
            id=row.id,
            name=row.name,
            id_number_masked=f"{row.id_number_hash[:6]}****",
            phone_masked=f"{row.phone_hash[:3]}****",
            email=row.email,
            email_verified=bool(row.email_verified),
            status=AccountStatus(row.status.value),
            created_at=row.created_at.replace(tzinfo=timezone.utc)
            if row.created_at and row.created_at.tzinfo is None
            else row.created_at,
            verified_at=_aware(row.verified_at),
            deletion_requested_at=_aware(row.deletion_requested_at),
            deletion_completed_at=_aware(row.deletion_completed_at),
            child_ids=[c.id for c in row.children],
            metadata=dict(row.parent_metadata or {}),
        )


def _aware(dt: datetime | None) -> datetime | None:
    """Coerce naive datetime to UTC-aware (DB may strip tzinfo on read)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# In-memory implementation (tests + dev fallback)
# ---------------------------------------------------------------------------


class InMemoryAccountRepository(AccountRepository):
    """Drop-in replacement for the legacy in-dict storage.

    Keeps the same semantics as the original `AccountLifecycle._accounts`
    dict so existing unit tests in `tests/auth/test_lifecycle.py` keep
    working without changes.
    """

    def __init__(self) -> None:
        self._parents: dict[str, ParentAccount] = {}
        self._phone_to_id: dict[str, str] = {}  # phone_hash -> parent_id (BE-AUTH-002)
        self._email_to_id: dict[str, str] = {}  # BE-AUTH-006
        # BE-AUTH-006: stash the raw phone alongside the masked view so
        # the phone-change service can do the "same number" check.
        # Production (Postgres) can't do this; the helper is a no-op
        # there and the check still passes because both sides go
        # through the same hash pipeline.
        self._raw_phones: dict[str, str] = {}
        self._progress: dict[str, "SignupProgress"] = {}

    async def get(self, parent_id: str) -> ParentAccount | None:
        return self._parents.get(parent_id)

    async def get_by_phone_hash(self, phone_hash: str) -> ParentAccount | None:
        """BE-AUTH-002: lookup account by phone hash (SMS login)."""
        parent_id = self._phone_to_id.get(phone_hash)
        if parent_id is None:
            return None
        return self._parents.get(parent_id)

    async def find_by_email_hash(self, email_hash: str) -> ParentAccount | None:
        parent_id = self._email_to_id.get(email_hash)
        if parent_id is None:
            return None
        return self._parents.get(parent_id)

    async def update_phone_hash(self, parent_id: str, new_phone: str) -> ParentAccount | None:
        account = self._parents.get(parent_id)
        if account is None:
            return None
        # Drop the OLD phone_hash index entry.
        for ph, pid in list(self._phone_to_id.items()):
            if pid == parent_id:
                self._phone_to_id.pop(ph, None)
        new_hash = self.hash_field(new_phone)
        self._phone_to_id[new_hash] = parent_id
        # BE-AUTH-006: keep the in-memory raw phone in sync.
        self._raw_phones[parent_id] = new_phone
        account.phone_masked = mask_phone(new_phone)
        return account

    def get_raw_phone(self, parent_id: str) -> str | None:
        """BE-AUTH-006: test/dev helper to fetch the raw phone."""
        return self._raw_phones.get(parent_id)

    async def update_email(self, parent_id: str, new_email: str) -> ParentAccount | None:
        account = self._parents.get(parent_id)
        if account is None:
            return None
        # Drop the OLD email_hash index entry.
        for eh, pid in list(self._email_to_id.items()):
            if pid == parent_id:
                self._email_to_id.pop(eh, None)
        new_hash = self.hash_field(new_email)
        self._email_to_id[new_hash] = parent_id
        account.email = new_email
        account.email_verified = True
        return account

    async def create(
        self,
        *,
        parent_id: str,
        name: str,
        id_number: str,
        phone: str,
        email: str | None = None,
    ) -> ParentAccount:
        from niki.auth.lifecycle import SignupProgress, SignupStep

        account = ParentAccount(
            id=parent_id,
            name=name,
            id_number_masked=mask_id_number(id_number),
            phone_masked=mask_phone(phone),
            email=email,
            email_verified=False,
            status=AccountStatus.PENDING,
        )
        self._parents[parent_id] = account
        # BE-AUTH-002: secondary index by phone hash for SMS-login lookup.
        self._phone_to_id[self.hash_field(phone)] = parent_id
        # BE-AUTH-006: stash the raw phone for same-number detection
        # (only meaningful in the in-memory backend).
        self._raw_phones[parent_id] = phone
        # BE-AUTH-006: secondary index by email hash for uniqueness check.
        if email is not None and email.strip():
            self._email_to_id[self.hash_field(email)] = parent_id
        self._progress[parent_id] = SignupProgress(
            parent_id=parent_id,
            steps=[
                SignupStep(STEP_REAL_NAME),
                SignupStep(STEP_FACE),
                SignupStep(STEP_CONSENT),
            ],
        )
        return account

    async def get_progress(self, parent_id: str) -> "SignupProgress | None":
        return self._progress.get(parent_id)

    async def mark_verification(
        self,
        parent_id: str,
        step: str,
        *,
        completed: bool,
        error: str | None = None,
    ) -> None:
        progress = self._progress.get(parent_id)
        if progress is None:
            return
        progress.mark_step(step, completed, error)

    async def set_verified(self, parent_id: str) -> None:
        acc = self._parents.get(parent_id)
        if acc is None:
            return
        acc.status = AccountStatus.VERIFIED
        acc.verified_at = datetime.now(timezone.utc)

    async def set_deletion_pending(self, parent_id: str) -> None:
        acc = self._parents.get(parent_id)
        if acc is None:
            return
        acc.status = AccountStatus.DELETION_PENDING
        acc.deletion_requested_at = datetime.now(timezone.utc)

    async def set_deleted(self, parent_id: str) -> None:
        acc = self._parents.get(parent_id)
        if acc is None:
            return
        acc.status = AccountStatus.DELETED
        acc.deletion_completed_at = datetime.now(timezone.utc)

    async def delete(self, parent_id: str) -> None:
        parent = self._parents.pop(parent_id, None)
        if parent is not None:
            # BE-AUTH-002: also drop the phone hash index entry.
            for ph, pid in list(self._phone_to_id.items()):
                if pid == parent_id:
                    self._phone_to_id.pop(ph, None)
            # BE-AUTH-006: also drop the email hash index entry.
            for eh, pid in list(self._email_to_id.items()):
                if pid == parent_id:
                    self._email_to_id.pop(eh, None)
        self._progress.pop(parent_id, None)

    async def list_all(self) -> list[ParentAccount]:
        return list(self._parents.values())


async def persist_parent_account_to_postgres(
    parent_id: str,
    name: str,
    id_number: str,
    phone: str,
    email: str | None = None,
) -> None:
    """Best-effort write-through for auto-created PENDING accounts."""
    from sqlalchemy import select

    from niki.infrastructure.postgres import PostgresClient

    if PostgresClient._engine is None:
        return
    async for session in PostgresClient.get_session():
        existing = (
            await session.execute(select(Parent).where(Parent.id == parent_id))
        ).scalar_one_or_none()
        if existing is None:
            parent = Parent(
                id=parent_id,
                name=name,
                id_number_hash=PostgresAccountRepository.hash_field(id_number),
                phone_hash=PostgresAccountRepository.hash_field(phone),
                email=email,
                email_hash=(PostgresAccountRepository.hash_field(email) if email else None),
                email_verified=False,
                status=ParentStatus.PENDING,
            )
            session.add(parent)
            await session.flush()
            for step_name in ALL_STEPS:
                session.add(
                    AccountVerification(
                        parent_id=parent_id,
                        step_name=step_name,
                        completed=False,
                        error=None,
                    )
                )
        await session.commit()
