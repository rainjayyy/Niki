"""Translation catalogs for the Niki backend (BE-I18N-001).

Catalogs are plain Python dicts keyed by dotted, namespace-qualified
keys (`auth.invalid_phone`, `media.file_not_found`).

Per-product convention (Chinese-first): `zh` is the *default* and
must contain every key. `en` may be partial during the rollout —
the `Translator` falls back to English, then to the key itself.

When adding a new key:
  1. Add the entry to `MESSAGES_ZH` (default).
  2. Mirror it in `MESSAGES_EN` (even if the English string is just
     the literal Chinese — this keeps the catalogs diffable and
     makes it obvious what is untranslated).
  3. Use the same dotted key in the API code.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Chinese (default) catalog
# ---------------------------------------------------------------------------

MESSAGES_ZH: dict[str, str] = {
    # ---- auth.* (BE-AUTH-001 / BE-AUTH-002 / BE-AUTH-003 / BE-AUTH-004) ----
    "auth.invalid_phone_format": "手机号格式不正确",
    "auth.token_expired": "登录已过期，请重新登录",
    "auth.token_invalid": "登录凭证无效",
    "auth.token_revoked": "登录凭证已被注销",
    "auth.missing_token": "缺少登录凭证",
    "auth.invalid_refresh_token": "刷新令牌无效",
    "auth.wrong_token_type": "令牌类型不正确",
    "auth.sms_rate_limited": "请求过于频繁，请稍后再试",
    "auth.sms_code_invalid": "验证码不正确",
    "auth.sms_code_expired": "验证码已过期，请重新获取",
    "auth.sms_too_many_attempts": "尝试次数过多，请重新获取验证码",
    "auth.captcha_invalid": "图形验证码错误或已过期",
    "auth.captcha_required": "发送短信前必须先完成图形验证码",
    "auth.signup_invalid_id_number": "身份证号格式不正确",
    "auth.signup_invalid_phone": "手机号格式不正确",
    "auth.signup_missing_field": "缺少必填字段",
    "auth.not_found": "账户不存在",
    "auth.invalid_consent_type": "无效的同意书类型",
    "auth.deletion_window_active": "账户处于删除冷静期，无法操作",
    "auth.consent_required": "请先签署知情同意书",
    "auth.parent_child_mismatch": "当前账户不能访问该孩子的数据",
    "auth.child_not_found": "未找到该孩子的注册信息",

    # ---- media.* (BE-MEDIA-001 / BE-MEDIA-001-rollout) ----
    "media.empty_file": "上传的文件为空",
    "media.file_not_found": "文件不存在或已过期",
    "media.invalid_mime_type": "不支持的文件类型",
    "media.file_too_large": "文件超过大小限制",
    "media.storage_unavailable": "存储服务暂时不可用",
    "media.invalid_thumb_size": "缩略图尺寸不合法",
    "media.invalid_device_id": "device_id 必须为 1-64 位 [A-Za-z0-9._-]",
    "media.invalid_channel": "channel 不在支持的取值范围内",
    "media.invalid_platform": "platform 不在支持的取值范围内",
    "media.invalid_push_token": "push_token 不合法或缺失",
    "media.invalid_quiet_hour": "静音时段必须为 0-23 的整数",
    "media.invalid_enabled_channels": "enabled_channels 必须是字符串列表",
    "media.invalid_high_risk_only": "high_risk_only 必须为布尔值",
    "media.invalid_severity": "severities 必须是 {info, warning, critical} 的子集",
    "media.invalid_event_type": "muted_event_types 必须是非空字符串列表",

    # ---- push.* (BE-PUSH-006 测试推送端点) ----
    "push.device_not_found": "未找到该设备，或该设备不属于当前账户",
    "push.test_rate_limited": "测试推送请求过于频繁，请稍后再试",

    # ---- dashboard.* (BE-DASH-001) ----
    "dashboard.invalid_template": "模板名称不合法",
    "dashboard.missing_field": "缺少必填字段",
    "dashboard.invalid_sort": "排序字段不合法",

    # ---- auth-audit.* (BE-AUTH-001-rollout-audit) ----
    "auth_audit.bad_request": "请求参数不合法",
    "auth_audit.invalid_sort": "排序字段不合法",

    # ---- audit.* (BE-COMP-004 统一审计日志) ----
    "audit.cross_parent_forbidden": "只能查询自己的审计记录",

    # ---- notifications.* (BE-PUSH-005 通知中心) ----
    "notifications.not_found": "通知不存在或已被删除",
    "notifications.invalid_cursor": "分页游标无效或已过期",
    "notifications.invalid_severity": "severity 取值不在支持的范围内",
}


# ---------------------------------------------------------------------------
# English catalog
# ---------------------------------------------------------------------------

MESSAGES_EN: dict[str, str] = {
    # ---- auth.* ----
    "auth.invalid_phone_format": "Invalid phone number format",
    "auth.token_expired": "Session expired, please sign in again",
    "auth.token_invalid": "Invalid authentication token",
    "auth.token_revoked": "Authentication token has been revoked",
    "auth.missing_token": "Missing authentication token",
    "auth.invalid_refresh_token": "Invalid refresh token",
    "auth.wrong_token_type": "Wrong token type",
    "auth.sms_rate_limited": "Too many requests, please try again later",
    "auth.sms_code_invalid": "Verification code is incorrect",
    "auth.sms_code_expired": "Verification code has expired",
    "auth.sms_too_many_attempts": "Too many failed attempts; please request a new code",
    "auth.captcha_invalid": "Captcha challenge missing, expired, or wrong",
    "auth.captcha_required": "Captcha is required to send an SMS code",
    "auth.signup_invalid_id_number": "Invalid national ID number format",
    "auth.signup_invalid_phone": "Invalid phone number format",
    "auth.signup_missing_field": "Missing required field",
    "auth.not_found": "Account not found",
    "auth.invalid_consent_type": "Invalid consent document type",
    "auth.deletion_window_active": "Account is in deletion cool-off period",
    "auth.consent_required": "Please sign the consent document first",
    "auth.parent_child_mismatch": "Token does not own this child_id",
    "auth.child_not_found": "No child registered with the given id",

    # ---- media.* ----
    "media.empty_file": "Uploaded file is empty",
    "media.file_not_found": "File not found or expired",
    "media.invalid_mime_type": "Unsupported file type",
    "media.file_too_large": "File exceeds size limit",
    "media.storage_unavailable": "Storage service is temporarily unavailable",
    "media.invalid_thumb_size": "Thumbnail size out of range",
    "media.invalid_device_id": "device_id must be 1-64 chars of [A-Za-z0-9._-]",
    "media.invalid_channel": "channel must be one of the supported values",
    "media.invalid_platform": "platform must be one of the supported values",
    "media.invalid_push_token": "push_token is missing or invalid",
    "media.invalid_quiet_hour": "Quiet hour must be an integer 0-23",
    "media.invalid_enabled_channels": "enabled_channels must be a list of strings",
    "media.invalid_high_risk_only": "high_risk_only must be a boolean",
    "media.invalid_severity": "severities must be a subset of {info, warning, critical}",
    "media.invalid_event_type": "muted_event_types must be a list of non-empty strings",

    # ---- push.* (BE-PUSH-006 test push endpoint) ----
    "push.device_not_found": (
        "Device not found or not owned by this account"
    ),
    "push.test_rate_limited": (
        "Too many test push requests, please retry later"
    ),

    # ---- dashboard.* ----
    "dashboard.invalid_template": "Invalid template name",
    "dashboard.missing_field": "Missing required field",
    "dashboard.invalid_sort": "Invalid sort field",

    # ---- auth-audit.* ----
    "auth_audit.bad_request": "Bad request",
    "auth_audit.invalid_sort": "Invalid sort field",

    # ---- audit.* (BE-COMP-004 unified audit log) ----
    "audit.cross_parent_forbidden": (
        "You may only query your own audit history"
    ),

    # ---- notifications.* (BE-PUSH-005 notification center) ----
    "notifications.not_found": (
        "Notification does not exist or has been deleted"
    ),
    "notifications.invalid_cursor": (
        "Pagination cursor is invalid or expired"
    ),
    "notifications.invalid_severity": (
        "severity value is not in the supported set"
    ),
}


# Map keyed by language tag. New languages register here; the
# `Translator` looks up catalogs via this mapping.
CATALOGS: dict[str, dict[str, str]] = {
    "zh": MESSAGES_ZH,
    "en": MESSAGES_EN,
}


def known_languages() -> tuple[str, ...]:
    """Return the language tags this build ships catalogs for."""
    return tuple(CATALOGS.keys())