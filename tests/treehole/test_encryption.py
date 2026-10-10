"""Tests for treehole AES-256-GCM encryption."""

import os

import pytest

from niki.treehole.encryption import TreeholeEncryption


@pytest.fixture
def encryption() -> TreeholeEncryption:
    """Use a deterministic key for tests."""
    return TreeholeEncryption(key=b"A" * 32)


class TestEncryption:
    def test_init_with_32_byte_key(self) -> None:
        e = TreeholeEncryption(key=b"X" * 32)
        assert e is not None

    def test_init_with_wrong_key_size_raises(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            TreeholeEncryption(key=b"short")

    def test_encrypt_decrypt_roundtrip(self, encryption: TreeholeEncryption) -> None:
        plaintext = "我今天很难过"
        ct = encryption.encrypt(plaintext)
        recovered = encryption.decrypt(ct)
        assert recovered == plaintext

    def test_encrypt_produces_different_ciphertext_each_time(
        self, encryption: TreeholeEncryption
    ) -> None:
        """GCM uses random nonce — same plaintext encrypts to different CTs."""
        plaintext = "hello"
        ct1 = encryption.encrypt(plaintext)
        ct2 = encryption.encrypt(plaintext)
        assert ct1 != ct2
        # Both decrypt to same plaintext
        assert encryption.decrypt(ct1) == plaintext
        assert encryption.decrypt(ct2) == plaintext

    def test_decrypt_with_wrong_key_fails(self) -> None:
        e1 = TreeholeEncryption(key=b"K" * 32)
        e2 = TreeholeEncryption(key=b"L" * 32)
        ct = e1.encrypt("secret")
        with pytest.raises(ValueError, match="decryption failed"):
            e2.decrypt(ct)

    def test_decrypt_with_wrong_aad_fails(self, encryption: TreeholeEncryption) -> None:
        ct = encryption.encrypt("secret", associated_data=b"child-1")
        with pytest.raises(ValueError, match="decryption failed"):
            encryption.decrypt(ct, associated_data=b"child-2")

    def test_encrypt_with_aad_succeeds_with_correct_aad(
        self, encryption: TreeholeEncryption
    ) -> None:
        ct = encryption.encrypt("secret", associated_data=b"child-1")
        assert encryption.decrypt(ct, associated_data=b"child-1") == "secret"

    def test_decrypt_invalid_base64_raises(self, encryption: TreeholeEncryption) -> None:
        with pytest.raises(ValueError, match="invalid base64"):
            encryption.decrypt("not-base64!!!")

    def test_decrypt_too_short_raises(self, encryption: TreeholeEncryption) -> None:
        import base64

        short = base64.b64encode(b"short").decode()
        with pytest.raises(ValueError, match="missing nonce"):
            encryption.decrypt(short)

    def test_ciphertext_is_base64(self, encryption: TreeholeEncryption) -> None:
        ct = encryption.encrypt("test")
        # Should be valid base64
        import base64

        base64.b64decode(ct)  # Should not raise

    def test_key_from_env(self) -> None:
        # Set a valid env var
        os.environ["AES_ENCRYPTION_KEY"] = "A" * 64
        e = TreeholeEncryption()  # Loads from env
        ct = e.encrypt("test")
        assert e.decrypt(ct) == "test"
        del os.environ["AES_ENCRYPTION_KEY"]

    def test_missing_env_fails_fast_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BE-SEC-AES-FAIL-FAST: ``TreeholeEncryption()`` (no
        explicit key) must fail fast when ``AES_ENCRYPTION_KEY``
        is unset and ``NIKI_TEST_DEV_KEY`` is not enabled.
        Previously this returned 32 zero bytes — a catastrophic
        key-confusion bug that would have let any attacker
        decrypt treehole payloads."""
        monkeypatch.delenv("AES_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("NIKI_TEST_DEV_KEY", raising=False)
        with pytest.raises(RuntimeError, match="AES_ENCRYPTION_KEY"):
            TreeholeEncryption()

    def test_test_dev_key_fallback_uses_nonzero_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``NIKI_TEST_DEV_KEY=1`` is set, the fallback key
        must be obviously fake (all-0xFF), never zero bytes —
        this prevents accidentally shipping a zero-byte key
        alongside a real key rotation."""
        monkeypatch.delenv("AES_ENCRYPTION_KEY", raising=False)
        monkeypatch.setenv("NIKI_TEST_DEV_KEY", "1")
        e = TreeholeEncryption()
        assert e._key == b"\xff" * 32  # noqa: SLF001
        # Sanity: dev key must NOT be all zeros.
        assert e._key != b"\x00" * 32  # noqa: SLF001

    def test_wrong_length_hex_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """64 hex chars = 32 bytes is required. Anything else
        must raise with a clear error."""
        monkeypatch.setenv("AES_ENCRYPTION_KEY", "A" * 60)  # 30 bytes
        with pytest.raises(ValueError, match="32 bytes"):
            TreeholeEncryption()

    def test_key_rotation(self, encryption: TreeholeEncryption) -> None:
        new = encryption.rotate_key(b"N" * 32)
        # New instance has different key — old ciphertexts cannot decrypt
        ct = encryption.encrypt("secret")
        with pytest.raises(ValueError):
            new.decrypt(ct)
        # New can encrypt/decrypt its own
        new_ct = new.encrypt("new secret")
        assert new.decrypt(new_ct) == "new secret"
