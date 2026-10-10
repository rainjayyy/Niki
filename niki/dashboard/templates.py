"""Report template variants + branding (BE-DASH-001-rollout).

Each template reuses the same `EmotionWeeklyReport` data but
selects a different tone, length, and detail level:

    CHILD       — default; balanced tone for ages 7-10
    PRESCHOOL   — emoji-heavy, short sentences for ages 3-6
    SCHOOL      — data-forward, Z-score visible for ages 11-12

`ReportBranding` carries optional logo + brand name so a
co-branded deployment (e.g. partner kindergarten) can render
its own header instead of Niki's.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

from niki.dashboard.report import (
    EmotionWeeklyReport,
)

logger = structlog.get_logger(__name__)


class ReportTemplate(StrEnum):
    """Which report layout to render."""

    CHILD = "child"
    PRESCHOOL = "preschool"
    SCHOOL = "school"


# Max safe base64 size for the logo image. 100 KB decodes to
# ~75 KB of binary, which fits comfortably as an embedded image
# in a reportlab document.
MAX_LOGO_BASE64_BYTES = 100 * 1024

# Brand-name length cap. Anything longer gets truncated with
# an ellipsis. Keeps the PDF header from wrapping.
MAX_BRAND_NAME_CHARS = 30


@dataclass(frozen=True)
class ReportBranding:
    """Header customisation for the PDF.

    `logo_b64` is a base64-encoded PNG/JPG (with or without the
    `data:image/...;base64,` prefix — both are accepted).
    `brand_color` is a hex color (`#RRGGBB`) used for accents.
    """

    brand_name: str | None = None
    logo_b64: str | None = None
    brand_color: str = "#5B6BFF"

    @classmethod
    def safe(
        cls,
        brand_name: str | None = None,
        logo_b64: str | None = None,
        brand_color: str = "#5B6BFF",
    ) -> "ReportBranding":
        """Build a Branding with safe defaults + length caps."""
        if brand_name and len(brand_name) > MAX_BRAND_NAME_CHARS:
            brand_name = brand_name[: MAX_BRAND_NAME_CHARS - 1] + "…"
        if logo_b64 and len(logo_b64) > MAX_LOGO_BASE64_BYTES:
            logger.warning(
                "dashboard.branding.logo_too_large",
                size=len(logo_b64),
                max=MAX_LOGO_BASE64_BYTES,
            )
            logo_b64 = None
        return cls(
            brand_name=brand_name,
            logo_b64=logo_b64,
            brand_color=brand_color,
        )

    def logo_bytes(self) -> bytes | None:
        """Decode the base64 logo to raw image bytes.

        Strips the optional `data:image/png;base64,` prefix so
        callers can pass either form.
        """
        if not self.logo_b64:
            return None
        b64 = self.logo_b64
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        try:
            return base64.b64decode(b64)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "dashboard.branding.logo_decode_failed",
                error=str(exc),
            )
            return None


# ---------------------------------------------------------------------------
# Per-template copy
# ---------------------------------------------------------------------------


def _template_text(
    template: ReportTemplate,
    report: EmotionWeeklyReport,
) -> dict[str, Any]:
    """Return template-specific text fragments.

    Each template picks a different headline / section
    phrasing / actions list. The TrendTable data is identical
    across templates — only copy changes.
    """
    if template is ReportTemplate.PRESCHOOL:
        return {
            "title_emoji": "🌈 ",
            "trend_label_extra": "✨",
            "discoveries_intro": "宝贝今天探索了：",
            "tips_intro": "小提示：",
            "footer_message": "💝 一起陪宝贝慢慢长大~",
        }
    if template is ReportTemplate.SCHOOL:
        return {
            "title_emoji": "",
            "trend_label_extra": "",
            "discoveries_intro": "本周关注领域：",
            "tips_intro": "给家长的建议：",
            "footer_message": "数据按本周 UTC 时间窗口统计。",
        }
    # CHILD — default
    return {
        "title_emoji": "🌟 ",
        "trend_label_extra": "",
        "discoveries_intro": "本周发现：",
        "tips_intro": "温馨提示：",
        "footer_message": "",
    }


# Pre-baked action suggestions per template, picked when the
# auto-generated ones are short.
_DEFAULT_ACTIONS_BY_TEMPLATE: dict[ReportTemplate, list[str]] = {
    ReportTemplate.PRESCHOOL: [
        "每天 10 分钟亲子共读📚",
    ],
    ReportTemplate.SCHOOL: [
        "保持固定的睡前 15 分钟对话时间",
        "关注屏幕时间外的情绪表达",
        "引导孩子用文字记录自己的心情",
    ],
    ReportTemplate.CHILD: [
        '周末可一起读"情绪绘本"',
        "每天 10 分钟亲子对话",
    ],
}


def get_default_actions(
    template: ReportTemplate,
    report: EmotionWeeklyReport,
) -> list[str]:
    """Template-specific default actions.

    Returns the report's `action_suggestions` when populated,
    otherwise falls back to the per-template defaults.
    """
    if report.action_suggestions:
        return list(report.action_suggestions)
    return list(_DEFAULT_ACTIONS_BY_TEMPLATE[template])
