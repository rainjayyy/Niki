"""Opaque cursor helpers for cursor-paginated list endpoints.

BE-PUSH-005 added cursor pagination to the notification center. The
cursor encodes the `(alert_id, created_at)` of the previous page's
last row so the next SELECT can use strict less-than to resume
without OFFSET scans.

The cursor is intentionally *opaque* to the client:
  - The payload is base64-encoded JSON (`{"id": "...", "ts": "..."}`).
  - The first 16 bytes of the HMAC-SHA256 signature are appended and
    also base64-encoded into the output. The HMAC key is a process
    secret (default: a stable module-level fallback so test runs are
    reproducible; production overrides via
    `NIKI_CURSOR_SECRET` env var or `set_cursor_secret(...)`).

A tampered cursor fails signature verification and the endpoint
returns 400 `INVALID_CURSOR` — clients cannot roll their own cursors
to enumerate alert ids.

The format is intentionally URL-safe (no `+`, `/`, `=`):
    base64url(payload_json) + "." + base64url(signature[:16])
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Module-level default key. Used when no env var / explicit override
# is set — keeps tests deterministic without a process-wide config
# dance. Production must set `NIKI_CURSOR_SECRET` so two replicas of
# the same service can each decode the other side's cursors.
_DEFAULT_SECRET = b"niki-cursor-default-secret-DO-NOT-USE-IN-PROD"
_SECRET: bytes = _DEFAULT_SECRET


def set_cursor_secret(secret: bytes | str) -> None:
    """Override the cursor HMAC secret (production + tests)."""
    global _SECRET
    if isinstance(secret, str):
        _SECRET = secret.encode("utf-8")
    else:
        _SECRET = secret


def _load_secret_from_env() -> bytes:
    val = os.environ.get("NIKI_CURSOR_SECRET")
    if val:
        return val.encode("utf-8")
    return _DEFAULT_SECRET


_SECRET = _load_secret_from_env()


class CursorError(ValueError):
    """Raised when a cursor cannot be decoded / verified.

    The endpoint catches this and surfaces a 400 INVALID_CURSOR.
    """


@dataclass(frozen=True)
class CursorPosition:
    """Decoded cursor payload: the (id, ts) of the previous last row."""

    id: str
    ts: datetime


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    # Re-pad to a multiple of 4 so url-safe decoders accept our
    # rstripped() output.
    pad = (-len(text)) % 4
    return base64.urlsafe_b64decode(text + ("=" * pad))


def _sign(raw: bytes) -> bytes:
    return hmac.new(_SECRET, raw, hashlib.sha256).digest()[:16]


def encode_cursor(alert_id: str, ts: datetime) -> str:
    """Encode `(alert_id, ts)` as an opaque cursor string."""
    payload = {"id": alert_id, "ts": ts.isoformat()}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = _sign(raw)
    return f"{_b64encode(raw)}.{_b64encode(sig)}"


def decode_cursor(cursor: str) -> CursorPosition:
    """Decode and verify `cursor`. Raises `CursorError` on failure.

    Failure modes (all → CursorError so the endpoint renders the same
    400 INVALID_CURSOR regardless of which check tripped):
      - not "<payload>.<sig>" shape
      - signature mismatch (tampered or wrong key)
      - JSON shape invalid (not {"id": str, "ts": iso str})
      - timestamp not parseable
    """
    if not isinstance(cursor, str) or "." not in cursor:
        raise CursorError("cursor malformed")
    parts = cursor.split(".", 1)
    if len(parts) != 2:
        raise CursorError("cursor malformed")
    payload_b64, sig_b64 = parts
    try:
        raw = _b64decode(payload_b64)
        sig = _b64decode(sig_b64)
    except Exception as exc:  # binascii errors etc.
        raise CursorError("cursor malformed") from exc
    expected = _sign(raw)
    if not hmac.compare_digest(sig, expected):
        raise CursorError("cursor signature mismatch")
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CursorError("cursor payload not json") from exc
    if not isinstance(data, dict):
        raise CursorError("cursor payload not object")
    alert_id = data.get("id")
    ts_raw = data.get("ts")
    if not isinstance(alert_id, str) or not isinstance(ts_raw, str):
        raise CursorError("cursor payload missing fields")
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError as exc:
        raise CursorError("cursor timestamp not iso") from exc
    # Be liberal on tz: treat naive timestamps as UTC.
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_UTC())
    return CursorPosition(id=alert_id, ts=ts)


def _UTC() -> Any:
    from datetime import timezone

    return timezone.utc


__all__ = [
    "CursorError",
    "CursorPosition",
    "decode_cursor",
    "encode_cursor",
    "set_cursor_secret",
]
