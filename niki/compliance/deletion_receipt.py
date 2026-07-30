"""Deletion receipt PDF (BE-COMP-005, PIPL Art. 47).

Renders a parent-facing PDF that certifies the deletion of
their personal information. The certificate includes:
  - request id (UUID)
  - parent id
  - requested_at / completed_at
  - sha256 hash of the canonical record text (tamper-evident)

Pure-Python via reportlab; no external fonts needed.
"""

from __future__ import annotations

import hashlib
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

import structlog

logger = structlog.get_logger(__name__)


_CJK_FONT = "STSong-Light"
_FONT_REGISTERED = False


def _ensure_cjk_font() -> None:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return
    pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
    _FONT_REGISTERED = True


def _canonical_text(
    request_id: str,
    parent_id: str,
    requested_at: datetime,
    completed_at: datetime | None,
) -> str:
    """Stable text used as the input to the tamper-evident hash.

    Must remain stable across runs / versions so a parent can
    re-verify the receipt months later.
    """
    completed = (
        completed_at.isoformat() if completed_at else "PENDING"
    )
    return (
        f"DELETION_CERTIFICATE\n"
        f"request_id={request_id}\n"
        f"parent_id={parent_id}\n"
        f"requested_at={requested_at.isoformat()}\n"
        f"completed_at={completed}\n"
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_deletion_receipt(
    *,
    request_id: str,
    parent_id: str,
    requested_at: datetime,
    completed_at: datetime | None,
) -> bytes:
    """Render the deletion-receipt PDF. Returns the encoded
    bytes (ready to send as application/pdf)."""
    _ensure_cjk_font()
    canonical = _canonical_text(
        request_id, parent_id, requested_at, completed_at
    )
    digest = _hash(canonical)

    base = getSampleStyleSheet()
    title = ParagraphStyle(
        "NikiTitle",
        parent=base["Title"],
        fontName=_CJK_FONT,
        fontSize=20,
        leading=26,
        textColor=colors.HexColor("#1A1A1A"),
        spaceAfter=10,
    )
    body = ParagraphStyle(
        "NikiBody",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=11,
        leading=16,
        textColor=colors.HexColor("#1A1A1A"),
    )
    meta = ParagraphStyle(
        "NikiMeta",
        parent=base["Normal"],
        fontName=_CJK_FONT,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#6B6B6B"),
    )

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Niki Deletion Certificate - {request_id}",
    )
    story: list = []
    story.append(Paragraph("Niki 数据删除凭证", title))
    story.append(Paragraph(
        "本凭证依据《个人信息保护法》§47 条出具，证明您的"
        "个人数据已按要求删除。",
        body,
    ))
    story.append(Spacer(1, 8))

    completed_str = (
        completed_at.isoformat() if completed_at else "PENDING"
    )
    table = Table(
        [
            ["字段", "值"],
            ["申请 ID (request_id)", request_id],
            ["家长 ID (parent_id)", parent_id],
            ["申请时间 (requested_at)", requested_at.isoformat()],
            ["完成时间 (completed_at)", completed_str],
            [
                "内容哈希 (sha256)",
                digest,
            ],
        ],
        colWidths=[55 * mm, 110 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _CJK_FONT),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E5E5E5")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 8))

    story.append(Paragraph(
        "本凭证由 Niki 自动生成。任何对上述内容的篡改都会"
        "导致 sha256 校验失败。如需申诉，可将本凭证提交至 "
        "support@niki.example。",
        meta,
    ))
    doc.build(story)
    return buf.getvalue()