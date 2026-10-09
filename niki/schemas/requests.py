"""Request body schemas — Pydantic models that front the high-traffic
endpoints so FastAPI validates them BEFORE the rate-limit decorator
fires and BEFORE the function body runs.

Why this module exists:
- Frontend testing surfaced a class of bugs where the endpoint
  signature was `payload: dict`. Body parsing via `payload["key"]`
  raises KeyError on missing fields, which FastAPI surfaces as a
  500 INTERNAL_ERROR (the user sees "An unexpected error occurred")
  instead of the proper 400/422 with a clear error code.
- Worse, when the endpoint also carries an `@limiter.limit(...)`
  decorator, bad requests are still counted toward the rate limit —
  so 4 invalid SMS requests in a row return 429 instead of the
  expected 400 INVALID_PHONE_FORMAT for the first 3.
- Both issues are fixed by declaring a Pydantic model: FastAPI
  validates the body BEFORE calling the function (which is where
  the rate-limit decorator is), so bad inputs are rejected at
  the validation layer and never touch the rate-limit counter.

The models here match the existing schema in `docs/api-endpoints.md`
and `docs/frontend-integration.md` §6.3 (error contract).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Match a Chinese mainland mobile number (11 digits, leading 1).
# Loosened compared to `validate_phone` in niki.auth.account so the
# endpoint doesn't 500 on format edge cases.
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class SMSSendRequest(BaseModel):
    """Body for `POST /api/v1/auth/sms/send`.

    `phone` is mandatory. `captcha_id` / `captcha_answer` are
    optional — BE-COMP-006 toggles captcha gating via
    `Settings.sms_captcha_required`.
    """

    phone: str = Field(
        ..., min_length=11, max_length=11, description="11-digit Chinese mobile number"
    )
    captcha_id: str | None = Field(None, description="BE-COMP-006 captcha token id")
    # Captcha answer is coerced from the puzzle's arithmetic result;
    # callers may send either an int (e.g. eval("3 + 4") → 7) or a string.
    captcha_answer: int | str | None = Field(None, description="BE-COMP-006 captcha answer")

    @field_validator("phone")
    @classmethod
    def _phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("must match 1[3-9]xxxxxxxxx")
        return v


class SMSLoginRequest(BaseModel):
    """Body for `POST /api/v1/auth/sms/login`."""

    phone: str = Field(..., min_length=11, max_length=11)
    code: str = Field(..., min_length=6, max_length=6, description="6-digit SMS verification code")

    @field_validator("phone")
    @classmethod
    def _phone_format(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("must match 1[3-9]xxxxxxxxx")
        return v

    @field_validator("code")
    @classmethod
    def _code_digits(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("must be 6 digits")
        return v


class RecordHighRiskRequest(BaseModel):
    """Body for `POST /api/v1/dashboard/alerts/record/high-risk`.

    `original_text` is the child's exact words (encrypted at rest
    in dashboard_alerts; never echoed in the audit log). The
    endpoint requires this so we can route a parent alert and
    trigger a safety push notification (BE-RT-002).
    """

    child_id: str = Field(..., min_length=1, description="Child whose signal triggered the record")
    original_text: str = Field(
        ..., min_length=1, max_length=2000, description="Child's exact words"
    )
    niki_response_summary: str | None = Field(
        None,
        max_length=2000,
        description="Short summary of how Niki responded. Defaults to a safe canned phrase when missing.",
    )


class RecordSafetyValveRequest(BaseModel):
    """Body for `POST /api/v1/dashboard/alerts/record/safety-valve`."""

    child_id: str = Field(..., min_length=1)
    problem_summary: str = Field(..., min_length=1, max_length=2000)
    round_reached: int = Field(
        4, ge=1, le=10, description="Which tutoring round the valve fired at (1-10)"
    )


class PushTestRequest(BaseModel):
    """Body for `POST /api/v1/push/test`.

    `device_id` is required and validated against the same regex as
    the device registration endpoint. All other fields are optional
    and fall back to safe defaults in the handler.
    """

    device_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Target device id (1-64 chars [A-Za-z0-9._-])",
    )
    title: str | None = Field(
        None, max_length=200, description="Notification title (default: '测试推送')"
    )
    body: str | None = Field(
        None, max_length=2000, description="Notification body (default: '这是一条测试推送')"
    )
    severity: str | None = Field(None, description="One of info|warning|critical (default: info)")
    dedupe_key: str | None = Field(
        None, description="Dedupe key — same key collapses to one notification"
    )
    data: dict | None = Field(None, description="Free-form payload attached to the notification")
