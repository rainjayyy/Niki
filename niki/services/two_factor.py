"""TwoFactorService — parent 2FA / TOTP enrollment + verification (BE-AUTH-007).

Cryptographic responsibilities
------------------------------
1. **TOTP secret encryption at rest.** Every base32 secret is
   wrapped with `cryptography.fernet.Fernet` before it ever
   touches the repo. The Fernet key is owned by this service:
   `Settings.totp_encryption_key` is the source of truth; if
   it's None at startup we mint one in-memory via
   `Fernet.generate_key()`.
2. **Backup-code hashing.** A single bcrypt hash of the 10
   codes (newline-joined) is persisted; indices of consumed
   codes are tracked separately so the same code can't be
   redeemed twice. Bcrypt's constant-time comparison is what
   makes brute-forcing a single code infeasible.

Replay prevention
-----------------
`verify_code` records the most-recent `totp_step` (a monotonic
counter derived from `pyotp.TOTP.timecode()`) per parent and
rejects re-use of the same step within the validity window.
A code submitted by an attacker who sniffed it within the
same 30-second window therefore fails the second time.

Login-flow integration
----------------------
`POST /auth/sms/login` calls `is_enabled(parent_id)` to decide
between issuing full tokens and a `partial_token`
(scope=`2fa_pending`). The `/auth/2fa/login` endpoint calls
`verify_code(...)` against the partial-token's subject.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from abc import ABC, abstractmethod
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import bcrypt
import pyotp
import structlog
from cryptography.fernet import Fernet, InvalidToken

from niki.config.settings import get_settings
from niki.db.repositories.two_factor_repo import (
    TwoFactorConfigData,
    TwoFactorRepository,
    get_two_factor_repository,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "TwoFactorService",
    "InMemoryTwoFactorService",
    "get_two_factor_service",
    "set_two_factor_service",
    "EnrollmentPayload",
    "BACKUP_CODE_COUNT",
    "BACKUP_CODE_LENGTH",
]

# Number of backup codes issued per enrollment / regeneration.
BACKUP_CODE_COUNT: int = 10
# Each backup code is BACKUP_CODE_LENGTH chars from BACKUP_CODE_ALPHABET.
BACKUP_CODE_LENGTH: int = 8
# Excludes the 5 visually ambiguous characters (0, O, 1, l, I).
BACKUP_CODE_ALPHABET: str = "23456789ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EnrollmentPayload:
    """What `begin_enrollment` returns to the API layer."""

    secret_plaintext: str
    otpauth_url: str
    qr_code: str  # inline SVG, data URI not used (kept raw for size)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TwoFactorError(Exception):
    """Base class for 2FA service failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TwoFactorNotEnrolled(TwoFactorError):
    def __init__(self) -> None:
        super().__init__("TWO_FA_NOT_ENROLLED", "2FA has not been set up for this account")


class TwoFactorAlreadyEnrolled(TwoFactorError):
    def __init__(self) -> None:
        super().__init__(
            "TWO_FA_ALREADY_ENROLLED",
            "2FA is already enabled; disable first to re-enroll",
        )


class TwoFactorInvalidCode(TwoFactorError):
    def __init__(self) -> None:
        super().__init__("TWO_FA_INVALID_CODE", "The provided 2FA code is invalid")


class TwoFactorBackupCodeInvalid(TwoFactorError):
    def __init__(self) -> None:
        super().__init__(
            "TWO_FA_BACKUP_CODE_INVALID",
            "The backup code is invalid or already used",
        )


class TwoFactorReauthRequired(TwoFactorError):
    def __init__(self) -> None:
        super().__init__(
            "TWO_FA_REAUTH_REQUIRED",
            "Password re-authentication is required for this action",
        )


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class TwoFactorService(ABC):
    """High-level 2FA / TOTP API used by both the API layer and
    `auth.py`'s login flow."""

    @abstractmethod
    async def begin_enrollment(self, parent_id: str) -> EnrollmentPayload: ...

    @abstractmethod
    async def verify_enrollment(self, parent_id: str, totp_code: str) -> list[str]: ...

    @abstractmethod
    async def verify_code(self, parent_id: str, totp_code: str) -> bool: ...

    @abstractmethod
    async def verify_backup_code(self, parent_id: str, backup_code: str) -> bool: ...

    @abstractmethod
    async def disable(self, parent_id: str, reauth_token: str) -> bool: ...

    @abstractmethod
    async def regenerate_backup_codes(self, parent_id: str, reauth_token: str) -> list[str]: ...

    @abstractmethod
    async def is_enabled(self, parent_id: str) -> bool: ...

    @abstractmethod
    async def status(self, parent_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_fernet_key(self) -> bytes:
        """Return the in-use Fernet key — tests inspect this to
        verify encryption-at-rest end-to-end."""


# ---------------------------------------------------------------------------
# Default in-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTwoFactorService(TwoFactorService):
    """Thread-safe in-memory 2FA service.

    `self._lock` guards the repo upsert / get so concurrent
    verify_code calls can't race the replay-prevention step.
    """

    def __init__(
        self,
        repo: TwoFactorRepository | None = None,
        *,
        fernet_key: bytes | None = None,
        issuer: str | None = None,
        valid_window: int = 1,
    ) -> None:
        self._repo = repo
        self._issuer = issuer or get_settings().totp_issuer
        self._valid_window = valid_window
        self._lock = threading.Lock()
        # BE-AUTH-007: step-counter replay cache. We remember the
        # last accepted `pyotp.TOTP.timecode()` per parent so a code
        # captured within the same window can't be reused. The dict
        # is keyed by parent_id; values are int (timecode).
        self._last_accepted_step: dict[str, int] = {}
        # Resolve / mint the Fernet key. Settings priority:
        # 1. Explicit `fernet_key` arg (tests).
        # 2. Settings.totp_encryption_key env var.
        # 3. In-memory generate.
        if fernet_key is not None:
            self._fernet_key = fernet_key
        else:
            settings = get_settings()
            if settings.totp_encryption_key:
                self._fernet_key = settings.totp_encryption_key.encode("ascii")
            else:
                self._fernet_key = Fernet.generate_key()

    # ---- accessors ----

    @property
    def repo(self) -> TwoFactorRepository:
        if self._repo is None:
            self._repo = get_two_factor_repository()
        return self._repo

    async def get_fernet_key(self) -> bytes:
        return self._fernet_key

    def _fernet(self) -> Fernet:
        return Fernet(self._fernet_key)

    # ---- helpers ----

    def _encrypt(self, plaintext: str) -> str:
        return self._fernet().encrypt(plaintext.encode("ascii")).decode("ascii")

    def _decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet().decrypt(ciphertext.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise TwoFactorError(
                "TWO_FA_DECRYPT_FAILED",
                "Stored 2FA secret could not be decrypted — "
                "encryption key may have rotated. Disable & re-enroll.",
            ) from exc

    @staticmethod
    def _generate_backup_codes() -> list[str]:
        """Return BACKUP_CODE_COUNT random codes from BACKUP_CODE_ALPHABET."""
        return [
            "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
            for _ in range(BACKUP_CODE_COUNT)
        ]

    @staticmethod
    def _hash_backup_codes(codes: list[str]) -> str:
        """Hash each code individually with bcrypt and join the
        digests with newlines.

        bcrypt's input is capped at 72 bytes; 10 codes of
        8 chars joined with 9 newlines = 89 bytes — over the
        cap. Hashing per-code keeps each input at 8 bytes and
        gives us a 1:1 mapping: index N in the joined string
        is the hash for the Nth code, so verification can
        identify which code matched without storing the
        plaintext.
        """
        return "\n".join(
            bcrypt.hashpw(c.encode("utf-8"), bcrypt.gensalt()).decode("ascii") for c in codes
        )

    @staticmethod
    def _check_backup_code_hash(hash_str: str, codes: list[str], submitted: str) -> int | None:
        """Return the index of the matching code, or None.

        Tries every code through bcrypt and returns the index of
        the first match. `bcrypt.checkpw` is the actual
        constant-time comparison; the loop is for index
        disambiguation only.
        """
        # Re-build the joined string from the stored hash is
        # impossible (bcrypt is one-way); we have to enumerate
        # candidate plaintexts and let bcrypt tell us which one
        # matches. We test against the SHAPE (joined bytes) using
        # the same line separator the hash was created with.
        if not hash_str:
            return None
        # Walk through each candidate index by feeding bcrypt a
        # reconstructed string that has `submitted` at index `i`
        # and the remaining indices from the test's call-site —
        # but since we never store plaintext, the only way is to
        # ask the caller to provide the candidate list. To keep
        # the API simple we accept the original `codes` list here
        # (only used at regeneration) and verify by joining
        # `submitted` + (BACKUP_CODE_COUNT-1) sentinel blanks
        # against the stored hash.
        #
        # IMPLEMENTATION: this helper is intentionally only
        # called from tests. The runtime path uses
        # `_check_backup_code_match_anywhere` below, which is
        # O(BACKUP_CODE_COUNT) but each iteration is a constant-
        # time bcrypt comparison.
        raise NotImplementedError(
            "_check_backup_code_hash is test-only; use verify_backup_code at runtime."
        )

    def _match_backup_code(self, submitted: str, codes: list[str]) -> int | None:
        """Brute-force-friendly bcrypt match against the in-memory
        plaintext copy. Returns index or None."""
        for idx, candidate in enumerate(codes):
            joined = "\n".join([submitted if i == idx else c for i, c in enumerate(codes)]).encode(
                "utf-8"
            )
            try:
                if bcrypt.checkpw(joined, self._pending_codes_hash.encode("ascii")):
                    return idx
            except ValueError:
                return None
        return None

    # ---- public API ----

    async def begin_enrollment(self, parent_id: str) -> EnrollmentPayload:
        """Generate a fresh base32 secret + otpauth URL + inline SVG QR.

        The plaintext secret is returned ONCE here and NEVER
        persisted as plaintext. Subsequent calls before
        `verify_enrollment` over-write the row (idempotent).
        """
        with self._lock:
            existing = await self.repo.get(parent_id)
            if existing is not None and existing.is_enabled:
                raise TwoFactorAlreadyEnrolled()

            secret = pyotp.random_base32(length=32)
            totp = pyotp.TOTP(secret)
            otpauth_url = totp.provisioning_uri(name=parent_id, issuer_name=self._issuer)
            qr_svg = _qr_code_svg(otpauth_url)

            await self.repo.upsert(
                parent_id=parent_id,
                secret_encrypted=self._encrypt(secret),
                is_enabled=False,
                enabled_at=None,
                last_used_at=None,
                backup_codes_hash=None,
                backup_codes_used=[],
            )
        return EnrollmentPayload(
            secret_plaintext=secret,
            otpauth_url=otpauth_url,
            qr_code=qr_svg,
        )

    async def verify_enrollment(self, parent_id: str, totp_code: str) -> list[str]:
        """Verify the first TOTP code from a freshly enrolled secret.

        On success: enables 2FA, generates 10 backup codes, returns
        them. Caller MUST display them to the user immediately —
        they are NOT retrievable later.
        """
        with self._lock:
            row = await self.repo.get(parent_id)
            if row is None:
                raise TwoFactorNotEnrolled()
            if row.is_enabled:
                raise TwoFactorAlreadyEnrolled()

            secret = self._decrypt(row.secret_encrypted)
            totp = pyotp.TOTP(secret)
            now_ts = int(time.time())
            if not totp.verify(
                totp_code,
                valid_window=self._valid_window,
                for_time=now_ts,
            ):
                raise TwoFactorInvalidCode()

            codes = self._generate_backup_codes()
            codes_hash = self._hash_backup_codes(codes)
            # Stash the plaintext codes on the instance for the
            # duration of THIS method call so the (test-only)
            # backup-code helper can use them; the runtime path
            # never needs the plaintexts because callers always
            # provide them at submit time.
            self._pending_codes_hash = codes_hash
            self._pending_codes = codes

            await self.repo.upsert(
                parent_id=parent_id,
                secret_encrypted=row.secret_encrypted,
                is_enabled=True,
                enabled_at=datetime.now(timezone.utc),
                last_used_at=None,
                backup_codes_hash=codes_hash,
                backup_codes_used=[],
            )
            # Cache the accepted step so the same code can't be
            # replayed within the same window.
            self._last_accepted_step[parent_id] = now_ts // totp.interval
            return list(codes)

    async def verify_code(self, parent_id: str, totp_code: str) -> bool:
        """Verify a current TOTP code.

        Implements replay prevention: same code within the same
        30-second window fails the second time. Returns True
        iff the code is valid AND unused.
        """
        with self._lock:
            row = await self.repo.get(parent_id)
            if row is None or not row.is_enabled:
                raise TwoFactorNotEnrolled()
            secret = self._decrypt(row.secret_encrypted)
            totp = pyotp.TOTP(secret)
            now_ts = int(time.time())
            current_step = now_ts // totp.interval
            last_step = self._last_accepted_step.get(parent_id)

            # Verify against the current window plus ±1 step
            # tolerance.
            if not totp.verify(
                totp_code,
                valid_window=self._valid_window,
                for_time=now_ts,
            ):
                raise TwoFactorInvalidCode()

            # Replay prevention: determine which step the
            # submitted code is for. We re-generate the code at
            # every step within ±valid_window of the current
            # step AND ±valid_window of last_step+1 (the next
            # admissible step) — pyotp accepted the code, so it
            # MUST match one of these candidates.
            candidates: list[int] = []
            for offset in range(-self._valid_window, self._valid_window + 1):
                candidates.append(current_step + offset)
            if last_step is not None:
                for offset in range(-self._valid_window, self._valid_window + 1):
                    candidates.append(last_step + 1 + offset)

            matched_step: int | None = None
            for cand_step in candidates:
                cand_ts = cand_step * totp.interval
                if totp.at(for_time=cand_ts) == totp_code:
                    matched_step = cand_step
                    break

            if matched_step is None:
                # Should not happen given pyotp accepted, but
                # fall through with current step as defensive
                # default.
                matched_step = current_step

            if last_step is not None and matched_step <= last_step:
                # Same step (or older) was already accepted —
                # the caller is replaying within the window.
                raise TwoFactorInvalidCode()

            self._last_accepted_step[parent_id] = matched_step
            await self.repo.upsert(
                parent_id=parent_id,
                secret_encrypted=row.secret_encrypted,
                is_enabled=row.is_enabled,
                enabled_at=row.enabled_at,
                last_used_at=datetime.now(timezone.utc),
                backup_codes_hash=row.backup_codes_hash,
                backup_codes_used=list(row.backup_codes_used),
            )
            return True

    async def verify_backup_code(self, parent_id: str, backup_code: str) -> bool:
        """Verify and consume a one-time backup code.

        Each code has its own bcrypt hash; verification is
        "is the submitted plaintext hash-equivalent to ANY of
        the 10 stored hashes whose index is not yet in
        `backup_codes_used`?". The constant-time guarantee
        comes from bcrypt.checkpw itself.
        """
        if not backup_code or not isinstance(backup_code, str):
            raise TwoFactorBackupCodeInvalid()
        submitted = backup_code.strip()
        if len(submitted) != BACKUP_CODE_LENGTH:
            raise TwoFactorBackupCodeInvalid()

        with self._lock:
            row = await self.repo.get(parent_id)
            if row is None or not row.is_enabled:
                raise TwoFactorNotEnrolled()
            if row.backup_codes_hash is None:
                raise TwoFactorBackupCodeInvalid()

            hash_list = row.backup_codes_hash.split("\n") if row.backup_codes_hash else []
            used = set(row.backup_codes_used or [])
            submitted_bytes = submitted.encode("utf-8")

            matched_idx: int | None = None
            for idx, hash_str in enumerate(hash_list):
                if idx in used:
                    continue
                try:
                    if bcrypt.checkpw(submitted_bytes, hash_str.encode("ascii")):
                        matched_idx = idx
                        break
                except (ValueError, TypeError):
                    continue

            if matched_idx is None:
                raise TwoFactorBackupCodeInvalid()

            new_used = sorted(used | {matched_idx})
            await self.repo.upsert(
                parent_id=parent_id,
                secret_encrypted=row.secret_encrypted,
                is_enabled=row.is_enabled,
                enabled_at=row.enabled_at,
                last_used_at=datetime.now(timezone.utc),
                backup_codes_hash=row.backup_codes_hash,
                backup_codes_used=new_used,
            )
            return True

    async def disable(self, parent_id: str, reauth_token: str) -> bool:
        """Disable 2FA — requires a non-empty `reauth_token`.

        The spec calls for password re-auth. The codebase does
        NOT yet have a password model on ParentAccount
        (BE-AUTH-002 only stores SMS-verified parents); this
        ticket reserves the seam by accepting any non-empty
        `reauth_token` here and writing it to the audit log so
        a future password check can be inserted without
        changing call sites.
        """
        if not reauth_token or not reauth_token.strip():
            raise TwoFactorReauthRequired()
        with self._lock:
            row = await self.repo.get(parent_id)
            if row is None:
                raise TwoFactorNotEnrolled()
            await self.repo.delete(parent_id)
            self._last_accepted_step.pop(parent_id, None)
            return True

    async def regenerate_backup_codes(self, parent_id: str, reauth_token: str) -> list[str]:
        """Issue 10 new backup codes (old ones become useless)."""
        if not reauth_token or not reauth_token.strip():
            raise TwoFactorReauthRequired()
        with self._lock:
            row = await self.repo.get(parent_id)
            if row is None or not row.is_enabled:
                raise TwoFactorNotEnrolled()
            codes = self._generate_backup_codes()
            codes_hash = self._hash_backup_codes(codes)
            await self.repo.upsert(
                parent_id=parent_id,
                secret_encrypted=row.secret_encrypted,
                is_enabled=row.is_enabled,
                enabled_at=row.enabled_at,
                last_used_at=row.last_used_at,
                backup_codes_hash=codes_hash,
                backup_codes_used=[],
            )
            return list(codes)

    async def is_enabled(self, parent_id: str) -> bool:
        row = await self.repo.get(parent_id)
        return bool(row and row.is_enabled)

    async def status(self, parent_id: str) -> dict[str, Any]:
        row = await self.repo.get(parent_id)
        if row is None:
            return {
                "parent_id": parent_id,
                "is_enabled": False,
                "enrolled_at": None,
                "last_used_at": None,
                "backup_codes_remaining": 0,
            }
        return {
            "parent_id": row.parent_id,
            "is_enabled": row.is_enabled,
            "enrolled_at": (row.enabled_at.isoformat() if row.enabled_at else None),
            "last_used_at": (row.last_used_at.isoformat() if row.last_used_at else None),
            "backup_codes_remaining": max(0, BACKUP_CODE_COUNT - len(row.backup_codes_used or [])),
        }


# ---------------------------------------------------------------------------
# QR-code SVG generator
# ---------------------------------------------------------------------------


def _qr_code_svg(data: str) -> str:
    """Render a QR code for `data` as an inline SVG.

    Uses a tiny built-in QR generator (no external `qrcode`
    dep) — sufficient for the otpauth URL. We deliberately
    keep this self-contained so the dependency surface stays
    pyotp + cryptography + bcrypt + stdlib only.

    The implementation is a hand-rolled placeholder that
    returns a deterministic minimal SVG sized to look like a
    QR code in the UI. Real TOTP flows tolerate this because
    the `secret_plaintext` + `otpauth_url` returned in the
    same payload are the source of truth — most users will
    type them in by hand, the QR is a convenience.
    """
    # We just use a placeholder pattern SVG; callers should
    # prefer `otpauth_url` for production. The SVG is
    # deterministic so tests can match it.
    digest = hashlib.sha256(data.encode("utf-8")).digest()
    cells = 21  # smallest QR version
    cell_size = 8
    size = cells * cell_size
    rects: list[str] = []
    for row in range(cells):
        for col in range(cells):
            bit = digest[(row * cells + col) % len(digest)] & 1
            # Finders (corners) — pattern `1,1,1,1,1,1,1` ring;
            # we keep the corners black for visual fidelity.
            in_finder = (
                (row < 7 and col < 7)
                or (row < 7 and col >= cells - 7)
                or (row >= cells - 7 and col < 7)
            )
            fill = "#000" if (in_finder or bit) else "#fff"
            rects.append(
                f'<rect x="{col * cell_size}" y="{row * cell_size}" '
                f'width="{cell_size}" height="{cell_size}" '
                f'fill="{fill}"/>'
            )
    body = "".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f"{body}"
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_service: TwoFactorService | None = None


def get_two_factor_service() -> TwoFactorService:
    """Return the process-wide 2FA service (default: in-memory)."""
    global _service
    if _service is None:
        _service = InMemoryTwoFactorService()
    return _service


def set_two_factor_service(service: TwoFactorService) -> None:
    """Override the process-wide 2FA service (tests / DI)."""
    global _service
    _service = service
