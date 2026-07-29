"""Weekly report PDF rendering (BE-DASH-001, BE-DASH-001-rollout).

Wraps `EmotionWeeklyReport` from `niki.dashboard.report` into a
parent-facing A4 PDF. Uses reportlab's built-in STSong-Light
CJK CID font so Chinese text renders without external font
files (no system-font dependency).

Templates (BE-DASH-001-rollout):
    - CHILD       — default, balanced tone
    - PRESCHOOL   — emoji-heavy, short sentences
    - SCHOOL      — data-forward

Branding (BE-DASH-001-rollout):
    - Optional logo (base64 image) in the header
    - Brand name (max 30 chars) above the title
    - Brand color (hex) for the title accent
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from niki.dashboard.report import (
    EmotionWeeklyReport,
    MoodTrend,
)
from niki.dashboard.templates import (
    MAX_LOGO_BASE64_BYTES,
    ReportBranding,
    ReportTemplate,
    _template_text,
    get_default_actions,
)

import structlog

logger = structlog.get_logger(__name__)


# Register the CJK font once on first import. reportlab's
# `registerFont` is idempotent — repeat calls are no-ops.
_CJK_FONT = "STSong-Light"
_FONT_REGISTERED = False


def _ensure_cjk_font() -> None:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    _FONT_REGISTERED = True


_TREND_LABEL = {
    MoodTrend.IMPROVING: "上升 ↗",
    MoodTrend.STABLE: "稳定 →",
    MoodTrend.NEEDS_ATTENTION: "需关注 ⚠",
}


def _safe_color(hex_color: str) -> colors.Color:
    """Best-effort: parse a hex color; fall back to indigo on error."""
    try:
        return colors.HexColor(hex_color)
    except Exception:  # pragma: no cover
        return colors.HexColor("#5B6BFF")


def _trend_color(trend: MoodTrend) -> colors.Color:
    if trend == MoodTrend.IMPROVING:
        return colors.HexColor("#3CB371")
    if trend == MoodTrend.NEEDS_ATTENTION:
        return colors.HexColor("#E68A00")
    return colors.HexColor("#6B6B6B")


def _build_styles(brand_color: colors.Color) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    _ensure_cjk_font()
    title = ParagraphStyle(
        "NikiTitle",
        parent=base["Title"],
        fontName=_CJK_FONT,
        fontSize=20,
        leading=26,
        textColor=brand_color,
        spaceAfter=4,
    )
    subtitle = ParagraphStyle(
        "NikiSubtitle",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#6B6B6B"),
        spaceAfter=16,
    )
    section = ParagraphStyle(
        "NikiSection",
        parent=base["Heading2"],
        fontName=_CJK_FONT,
        fontSize=14,
        leading=18,
        textColor=brand_color,
        spaceBefore=12,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "NikiBody",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#1A1A1A"),
        spaceAfter=4,
    )
    bullet = ParagraphStyle(
        "NikiBullet",
        parent=body,
        leftIndent=14,
        bulletIndent=2,
        spaceAfter=2,
    )
    meta = ParagraphStyle(
        "NikiMeta",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#6B6B6B"),
    )
    brand_name_style = ParagraphStyle(
        "NikiBrand",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=12,
        leading=14,
        textColor=brand_color,
        spaceAfter=2,
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "section": section,
        "body": body,
        "bullet": bullet,
        "meta": meta,
        "brand_name": brand_name_style,
    }


class WeeklyReportPDF:
    """Render an `EmotionWeeklyReport` to PDF bytes.

    Stateless — `render()` returns the encoded PDF as `bytes`.

    Args:
        child_id:  the child's identifier (used for the title)
        report:    the structured EmotionWeeklyReport
        template:  one of ReportTemplate — controls tone / emoji / detail
        branding:  ReportBranding — optional logo + brand name + color
    """

    LOGO_MAX_WIDTH_MM = 24
    LOGO_MAX_HEIGHT_MM = 12

    def __init__(
        self,
        *,
        child_id: str,
        report: EmotionWeeklyReport,
        template: ReportTemplate = ReportTemplate.CHILD,
        branding: ReportBranding | None = None,
    ) -> None:
        self.child_id = child_id
        self.report = report
        self.template = template
        self.branding = branding or ReportBranding()

    def render(self) -> bytes:
        styles = _build_styles(
            _safe_color(self.branding.brand_color)
        )
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=(
                f"Niki 周报 - {self.branding.brand_name or 'Niki'} "
                f"- {self.child_id}"
            ),
        )
        story = self._build_story(styles)
        doc.build(story)
        return buf.getvalue()

    def _build_story(
        self, styles: dict[str, ParagraphStyle]
    ) -> list:
        story: list = []
        report = self.report
        trend = report.trend
        copy = _template_text(self.template, report)

        # ---- Brand header (logo + brand name) ----
        if self.branding.brand_name or self.branding.logo_b64:
            story.extend(self._build_header(styles))
            story.append(Spacer(1, 4))

        # ---- Title block ----
        title_emoji = copy["title_emoji"]
        story.append(
            Paragraph(f"{title_emoji}Niki 周报", styles["title"])
        )
        story.append(
            Paragraph(
                f"孩子 ID: {self.child_id} &nbsp;|&nbsp; "
                f"周期: {report.week_start} ~ {report.week_end}",
                styles["subtitle"],
            )
        )

        # ---- Headline ----
        story.append(
            Paragraph("本周亮点", styles["section"])
        )
        story.append(
            Paragraph(report.headline or "—", styles["body"])
        )
        story.append(Spacer(1, 6))

        # ---- Trend ----
        story.append(
            Paragraph("情绪趋势", styles["section"])
        )
        trend_label = _TREND_LABEL.get(
            trend.overall_trend, "—"
        ) + copy["trend_label_extra"]
        trend_table = Table(
            [
                ["整体趋势", "正向天数", "情绪低谷", "数据窗口"],
                [
                    trend_label,
                    f"{trend.positive_days} 天",
                    f"{trend.negative_dips} 次",
                    f"{(report.week_end, report.week_start)}",
                ],
            ],
            colWidths=[40 * mm, 35 * mm, 35 * mm, 60 * mm],
        )
        trend_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("BACKGROUND", (0, 0), (-1, 0),
                     colors.HexColor("#E5E5E5")),
                    ("TEXTCOLOR", (1, 1), (1, 1),
                     _trend_color(trend.overall_trend)),
                    ("TEXTCOLOR", (0, 1), (0, 1),
                     _trend_color(trend.overall_trend)),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(trend_table)
        story.append(Spacer(1, 6))

        # ---- Discoveries ----
        if report.discoveries:
            story.append(
                Paragraph(
                    copy["discoveries_intro"], styles["section"]
                )
            )
            for d in report.discoveries:
                story.append(
                    Paragraph(f"• {d}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # ---- Fuzzy negatives ----
        if report.fuzzy_negatives:
            story.append(
                Paragraph("温馨提示", styles["section"])
            )
            for n in report.fuzzy_negatives:
                story.append(
                    Paragraph(f"• {n}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # ---- Anomalies ----
        if report.anomaly_annotations:
            story.append(
                Paragraph("关注点", styles["section"])
            )
            for a in report.anomaly_annotations:
                story.append(
                    Paragraph(f"⚠ {a}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # ---- Action suggestions (template-aware) ----
        actions = get_default_actions(self.template, report)
        if actions:
            story.append(
                Paragraph(
                    copy["tips_intro"], styles["section"]
                )
            )
            for s in actions:
                story.append(
                    Paragraph(f"• {s}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # ---- Footer ----
        story.append(Spacer(1, 10))
        footer = (
            f"生成时间: {self._fmt_ts(report.generated_at)} &nbsp;|&nbsp; "
            f"覆盖 {self._window_days()} 天 &nbsp;|&nbsp; "
            f"安全阀触发 {report.safety_valve_count} 次 &nbsp;|&nbsp; "
            f"心洞模式 {report.treehole_engagement_count} 次"
        )
        if copy["footer_message"]:
            footer = f"{copy['footer_message']} &nbsp;|&nbsp; " + footer
        story.append(Paragraph(footer, styles["meta"]))
        return story

    def _build_header(
        self, styles: dict[str, ParagraphStyle]
    ) -> list:
        """Logo (left) + brand name (right) header row."""
        logo_bytes = self.branding.logo_bytes()
        if logo_bytes:
            try:
                logo_buf = io.BytesIO(logo_bytes)
                # Try to read image dimensions to compute a
                # sensible aspect ratio.
                try:
                    from PIL import Image as PILImage
                    with PILImage.open(logo_buf) as im:
                        w_px, h_px = im.size
                except Exception:
                    w_px, h_px = 100, 50
                logo_buf.seek(0)
                # Scale to fit LOGO_MAX_WIDTH_MM, preserve ratio
                aspect = (
                    h_px / w_px if w_px else 0.5
                )
                w_mm = self.LOGO_MAX_WIDTH_MM
                h_mm = min(
                    self.LOGO_MAX_HEIGHT_MM,
                    w_mm * aspect,
                )
                logo_img = Image(logo_buf, width=w_mm, height=h_mm)
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "dashboard.pdf.logo_render_failed",
                    error=str(exc),
                )
                logo_img = None
        else:
            logo_img = None

        if logo_img and self.branding.brand_name:
            row = Table(
                [[logo_img, Paragraph(
                    self.branding.brand_name,
                    styles["brand_name"],
                )]],
                colWidths=[
                    self.LOGO_MAX_WIDTH_MM + 4,
                    A4[0] - 18 * 2 - 4 - self.LOGO_MAX_WIDTH_MM,
                ],
            )
            row.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            return [row]
        if logo_img:
            return [logo_img]
        if self.branding.brand_name:
            return [Paragraph(
                self.branding.brand_name, styles["brand_name"]
            )]
        return []

    def _window_days(self) -> int:
        try:
            start = datetime.fromisoformat(self.report.week_start)
            end = datetime.fromisoformat(self.report.week_end)
            return max(1, (end - start).days)
        except (TypeError, ValueError):
            return 7

    @staticmethod
    def _fmt_ts(ts: datetime | None) -> str:
        if ts is None:
            return "—"
        try:
            return ts.strftime("%Y-%m-%d %H:%M UTC")
        except AttributeError:
            return str(ts)


def render_weekly_report_pdf(
    *,
    child_id: str,
    report: EmotionWeeklyReport,
    template: ReportTemplate = ReportTemplate.CHILD,
    branding: ReportBranding | None = None,
) -> bytes:
    """Module-level helper: render a report to PDF bytes."""
    return WeeklyReportPDF(
        child_id=child_id,
        report=report,
        template=template,
        branding=branding,
    ).render()