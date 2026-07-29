"""Weekly report PDF rendering (BE-DASH-001).

Wraps `EmotionWeeklyReport` from `niki.dashboard.report` into a
parent-facing A4 PDF. Uses reportlab's built-in STSong-Light
CJK CID font so Chinese text renders without external font
files (no system-font dependency).

Output is plain PDF bytes; the API layer is responsible for
returning it as `application/pdf` with a sensible
`Content-Disposition`.
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


# Colour palette (kept minimal — single accent + neutrals).
COLOR_ACCENT = colors.HexColor("#5B6BFF")     # Niki indigo
COLOR_POSITIVE = colors.HexColor("#3CB371")
COLOR_ATTENTION = colors.HexColor("#E68A00")
COLOR_TEXT = colors.HexColor("#1A1A1A")
COLOR_SUBTLE = colors.HexColor("#6B6B6B")
COLOR_RULE = colors.HexColor("#E5E5E5")


def _build_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    _ensure_cjk_font()
    title = ParagraphStyle(
        "NikiTitle",
        parent=base["Title"],
        fontName=_CJK_FONT,
        fontSize=20,
        leading=26,
        textColor=COLOR_ACCENT,
        spaceAfter=4,
    )
    subtitle = ParagraphStyle(
        "NikiSubtitle",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=10,
        leading=14,
        textColor=COLOR_SUBTLE,
        spaceAfter=16,
    )
    section = ParagraphStyle(
        "NikiSection",
        parent=base["Heading2"],
        fontName=_CJK_FONT,
        fontSize=14,
        leading=18,
        textColor=COLOR_ACCENT,
        spaceBefore=12,
        spaceAfter=6,
    )
    body = ParagraphStyle(
        "NikiBody",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=11,
        leading=16,
        textColor=COLOR_TEXT,
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
        textColor=COLOR_SUBTLE,
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "section": section,
        "body": body,
        "bullet": bullet,
        "meta": meta,
    }


_TREND_LABEL = {
    MoodTrend.IMPROVING: "上升 ↗",
    MoodTrend.STABLE: "稳定 →",
    MoodTrend.NEEDS_ATTENTION: "需关注 ⚠",
}


def _trend_color(trend: MoodTrend) -> colors.Color:
    if trend == MoodTrend.IMPROVING:
        return COLOR_POSITIVE
    if trend == MoodTrend.NEEDS_ATTENTION:
        return COLOR_ATTENTION
    return COLOR_SUBTLE


class WeeklyReportPDF:
    """Render an `EmotionWeeklyReport` to PDF bytes.

    Stateless — `render()` returns the encoded PDF as `bytes`.
    """

    def __init__(self, *, child_id: str, report: EmotionWeeklyReport) -> None:
        self.child_id = child_id
        self.report = report

    def render(self) -> bytes:
        """Build and return the PDF as a `bytes` blob."""
        styles = _build_styles()
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=18 * mm,
            bottomMargin=18 * mm,
            title=f"Niki 周报 - {self.child_id}",
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

        # Title block
        story.append(Paragraph("Niki 周报", styles["title"]))
        story.append(
            Paragraph(
                f"孩子 ID: {self.child_id} &nbsp;|&nbsp; "
                f"周期: {report.week_start} ~ {report.week_end}",
                styles["subtitle"],
            )
        )

        # Headline
        story.append(Paragraph("本周亮点", styles["section"]))
        story.append(Paragraph(report.headline or "—", styles["body"]))
        story.append(Spacer(1, 6))

        # Trend
        story.append(Paragraph("情绪趋势", styles["section"]))
        trend_label = _TREND_LABEL.get(trend.overall_trend, "—")
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
                    ("BACKGROUND", (0, 0), (-1, 0), COLOR_RULE),
                    ("TEXTCOLOR", (1, 1), (1, 1), _trend_color(trend.overall_trend)),
                    ("TEXTCOLOR", (0, 1), (0, 1), _trend_color(trend.overall_trend)),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(trend_table)
        story.append(Spacer(1, 6))

        # Discoveries
        story.append(Paragraph("本周发现", styles["section"]))
        if report.discoveries:
            for d in report.discoveries:
                story.append(
                    Paragraph(f"• {d}", styles["bullet"])
                )
        else:
            story.append(Paragraph("—", styles["body"]))
        story.append(Spacer(1, 6))

        # Fuzzy negatives
        story.append(Paragraph("温馨提示", styles["section"]))
        if report.fuzzy_negatives:
            for n in report.fuzzy_negatives:
                story.append(
                    Paragraph(f"• {n}", styles["bullet"])
                )
        else:
            story.append(Paragraph("本周状态平稳～", styles["body"]))
        story.append(Spacer(1, 6))

        # Anomalies
        if report.anomaly_annotations:
            story.append(Paragraph("关注点", styles["section"]))
            for a in report.anomaly_annotations:
                story.append(
                    Paragraph(f"⚠ {a}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # Action suggestions
        if report.action_suggestions:
            story.append(Paragraph("行动建议", styles["section"]))
            for s in report.action_suggestions:
                story.append(
                    Paragraph(f"• {s}", styles["bullet"])
                )
            story.append(Spacer(1, 6))

        # Footer / metadata
        story.append(Spacer(1, 10))
        story.append(
            Paragraph(
                f"生成时间: {self._fmt_ts(report.generated_at)} &nbsp;|&nbsp; "
                f"覆盖 {self._window_days()} 天 &nbsp;|&nbsp; "
                f"安全阀触发 {report.safety_valve_count} 次 &nbsp;|&nbsp; "
                f"心洞模式 {report.treehole_engagement_count} 次",
                styles["meta"],
            )
        )
        return story

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
    *, child_id: str, report: EmotionWeeklyReport
) -> bytes:
    """Module-level helper: render a report to PDF bytes.

    Equivalent to `WeeklyReportPDF(child_id=..., report=...).render()`
    but easier to call from the API layer.
    """
    return WeeklyReportPDF(child_id=child_id, report=report).render()