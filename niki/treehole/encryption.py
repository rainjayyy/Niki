"""Treehole AES-256-GCM encryption — PRD #62.

Encrypts the emotion summary before persisting to storage.
The key comes from environment variable (AES_ENCRYPTION_KEY, 32 bytes hex).
Production: key rotation handled externally; this class is stateless.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = structlog.get_logger(__name__)


# PRD: AES-256 = 32-byte key, hex-encoded
KEY_LENGTH_BYTES = 32


class TreeholeEncryption:
    """AES-256-GCM encryption for treehole emotion summaries.

    In production, the key is loaded from environment variable
    `AES_ENCRYPTION_KEY` (64 hex chars = 32 bytes).
    """

    def __init__(self, key: bytes | None = None) -> None:
        if key is None:
            key = self._load_key_from_env()
        if len(key) != KEY_LENGTH_BYTES:
            raise ValueError(
                f"AES-256 key must be {KEY_LENGTH_BYTES} bytes "
                f"(got {len(key)}). Set AES_ENCRYPTION_KEY env var."
            )
        self._cipher = AESGCM(key)
        self._key = key

    @staticmethod
    def _load_key_from_env() -> bytes:
        """Load the encryption key from AES_ENCRYPTION_KEY env var.

        Production: set ``AES_ENCRYPTION_KEY`` to a 64-char hex
        string (32 bytes for AES-256-GCM). The function fails
        fast on missing / malformed keys rather than falling
        back to a zero-byte key — a zero-byte key would let any
        attacker trivially decrypt treehole payloads.

        Test mode: the ``NIKI_TEST_DEV_KEY`` env var (any truthy
        value) restores the old dev-key fallback so the test
        suite can still construct ``TreeholeEncryption()``
        without provisioning real keys. The dev key is the
        single byte 0xFF repeated 32 times (clearly fake,
        impossible to confuse with a real production key).
        """
        hex_key = os.environ.get("AES_ENCRYPTION_KEY", "")
        if not hex_key:
            if os.environ.get("NIKI_TEST_DEV_KEY", "").lower() in ("1", "true", "yes"):
                # Deterministic dev key — clearly fake (all-0xFF).
                return b"\xff" * KEY_LENGTH_BYTES
            raise RuntimeError(
                "AES_ENCRYPTION_KEY is not set. Treehole encryption requires a "
                "64-char hex string (32 bytes for AES-256-GCM). Set it in your "
                "environment, or set NIKI_TEST_DEV_KEY=1 in test environments "
                "to use a deterministic dev fallback."
            )
        try:
            key = bytes.fromhex(hex_key)
        except ValueError as exc:
            raise ValueError(
                "AES_ENCRYPTION_KEY must be a valid hex string of 64 chars (32 bytes)"
            ) from exc
        if len(key) != KEY_LENGTH_BYTES:
            raise ValueError(
                f"AES_ENCRYPTION_KEY must decode to {KEY_LENGTH_BYTES} bytes "
                f"(got {len(key)}). For AES-256-GCM use 64 hex chars."
            )
        return key

    def encrypt(self, plaintext: str, associated_data: bytes | None = None) -> str:
        """Encrypt a plaintext string.

        Args:
            plaintext: The string to encrypt (e.g. JSON-serialised summary).
            associated_data: Optional AAD (e.g. child_id).

        Returns:
            Base64-encoded nonce + ciphertext (nonce || ct).
        """
        # Random nonce per encryption (PRD #62: GCM nonce uniqueness)
        import secrets

        nonce = secrets.token_bytes(12)  # 96-bit nonce for GCM
        ct = self._cipher.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, ciphertext: str, associated_data: bytes | None = None) -> str:
        """Decrypt a ciphertext string.

        Args:
            ciphertext: Base64-encoded nonce + ciphertext.
            associated_data: Same AAD used during encryption.

        Returns:
            Original plaintext.

        Raises:
            ValueError: If the ciphertext is malformed or auth fails.
        """
        try:
            data = base64.b64decode(ciphertext)
        except Exception as exc:
            raise ValueError("invalid base64 ciphertext") from exc

        if len(data) < 12:
            raise ValueError("ciphertext too short (missing nonce)")

        nonce, ct = data[:12], data[12:]
        try:
            plaintext = self._cipher.decrypt(nonce, ct, associated_data)
        except Exception as exc:
            raise ValueError("decryption failed (bad key or tampered ciphertext)") from exc

        return plaintext.decode("utf-8")

    def rotate_key(self, new_key: bytes) -> TreeholeEncryption:
        """Return a new instance with a rotated key (for key rotation ops)."""
        return TreeholeEncryption(key=new_key)


@dataclass
class EncryptedPayload:
    """Represents an encrypted treehole record (for storage)."""

    nonce_b64: str
    ciphertext_b64: str
    associated_data_b64: str | None
