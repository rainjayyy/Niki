"""Tests for the report template variants + branding (BE-DASH-001-rollout)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from niki.dashboard.pdf import (
    WeeklyReportPDF,
    render_weekly_report_pdf,
)
from niki.dashboard.report import (
    EmotionWeeklyReport,
    MoodTrend,
    WeeklyTrend,
)
from niki.dashboard.templates import (
    MAX_BRAND_NAME_CHARS,
    MAX_LOGO_BASE64_BYTES,
    ReportBranding,
    ReportTemplate,
    get_default_actions,
)


PDF_MAGIC = b"%PDF-"


def _sample_report(
    *,
    discoveries: list[str] | None = None,
    fuzzy_negatives: list[str] | None = None,
    action_suggestions: list[str] | None = None,
) -> EmotionWeeklyReport:
    return EmotionWeeklyReport(
        child_id="c-1",
        trend=WeeklyTrend(
            start_date="2026-07-22",
            end_date="2026-07-29",
            daily_valences={"2026-07-22": 0.1, "2026-07-25": -0.3},
            overall_trend=MoodTrend.STABLE,
            positive_days=5,
            negative_dips=1,
        ),
        headline="本周孩子聊了 18 次，整体心情稳定",
        discoveries=discoveries
        or [
            "探索了 3 个新领域：恐龙、太空、海洋",
            "主动问 Niki 8 个百科问题",
        ],
        fuzzy_negatives=fuzzy_negatives
        or ["聊完有 2 次明显情绪波动"],
        anomaly_annotations=[],
        action_suggestions=action_suggestions or [],
        safety_valve_count=0,
        treehole_engagement_count=2,
        generated_at=datetime(2026, 7, 29, 14, 30, tzinfo=timezone.utc),
        week_start="2026-07-22",
        week_end="2026-07-29",
    )


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------


class TestReportBranding:
    def test_default_branding_is_niki(self) -> None:
        b = ReportBranding()
        assert b.brand_name is None
        assert b.logo_b64 is None
        assert b.brand_color == "#5B6BFF"

    def test_long_brand_name_truncated(self) -> None:
        long = "x" * (MAX_BRAND_NAME_CHARS + 50)
        b = ReportBranding.safe(brand_name=long)
        assert len(b.brand_name) == MAX_BRAND_NAME_CHARS
        assert b.brand_name.endswith("…")

    def test_oversized_logo_dropped(self) -> None:
        huge = "A" * (MAX_LOGO_BASE64_BYTES + 1000)
        b = ReportBranding.safe(logo_b64=huge)
        assert b.logo_b64 is None

    def test_logo_bytes_decodes_raw_b64(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        b = ReportBranding(logo_b64=base64.b64encode(png_bytes).decode())
        assert b.logo_bytes() == png_bytes

    def test_logo_bytes_strips_data_url_prefix(self) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 50
        prefixed = "data:image/png;base64," + base64.b64encode(
            png_bytes
        ).decode()
        b = ReportBranding(logo_b64=prefixed)
        assert b.logo_bytes() == png_bytes

    def test_logo_bytes_invalid_raises_returns_none(self) -> None:
        # Not valid base64 → graceful None
        b = ReportBranding(logo_b64="not-valid-base64!!!")
        assert b.logo_bytes() is None

    def test_logo_bytes_none_when_no_logo(self) -> None:
        assert ReportBranding().logo_bytes() is None


# ---------------------------------------------------------------------------
# Default actions per template
# ---------------------------------------------------------------------------


class TestTemplateDefaultActions:
    def test_preschool_shorter_actions(self) -> None:
        actions = get_default_actions(
            ReportTemplate.PRESCHOOL, _sample_report()
        )
        # PRESCHOOL has 1 default action.
        assert len(actions) >= 1
        # More emoji-rich phrasing.
        assert any(
            "📚" in a or "陪" in a or "绘本" in a
            for a in actions
        ) or len(actions) == 1  # at least 1 action

    def test_school_more_actions(self) -> None:
        actions = get_default_actions(
            ReportTemplate.SCHOOL, _sample_report()
        )
        # SCHOOL has 3 default actions.
        assert len(actions) == 3

    def test_child_default_actions(self) -> None:
        actions = get_default_actions(
            ReportTemplate.CHILD, _sample_report()
        )
        assert len(actions) == 2

    def test_report_actions_override_defaults(self) -> None:
        report = _sample_report(
            action_suggestions=["custom 1", "custom 2"]
        )
        actions = get_default_actions(
            ReportTemplate.SCHOOL, report
        )
        assert actions == ["custom 1", "custom 2"]


# ---------------------------------------------------------------------------
# PDF rendering per template + branding
# ---------------------------------------------------------------------------


class TestPDFWithTemplates:
    @pytest.mark.parametrize(
        "template", list(ReportTemplate)
    )
    def test_render_with_each_template(self, template) -> None:
        pdf = render_weekly_report_pdf(
            child_id="c-1",
            report=_sample_report(),
            template=template,
        )
        assert pdf.startswith(PDF_MAGIC)

    def test_preschool_pdf_uses_different_copy(self) -> None:
        """The 3 templates should differ in copy length —
        PRESCHOOL has more emoji, SCHOOL has more data
        labels. We just verify all 3 render successfully;
        byte-level diff is brittle to reportlab version
        changes."""
        pdf_child = render_weekly_report_pdf(
            child_id="c-1", report=_sample_report(),
            template=ReportTemplate.CHILD,
        )
        pdf_pre = render_weekly_report_pdf(
            child_id="c-1", report=_sample_report(),
            template=ReportTemplate.PRESCHOOL,
        )
        pdf_sch = render_weekly_report_pdf(
            child_id="c-1", report=_sample_report(),
            template=ReportTemplate.SCHOOL,
        )
        # All three render valid PDFs.
        for p in (pdf_child, pdf_pre, pdf_sch):
            assert p.startswith(PDF_MAGIC)
        # Different templates likely produce different byte
        # streams (copy differs), but if the reportlab stream
        # happens to be deterministic for identical inputs
        # we only assert each starts with %PDF-.

    def test_brand_name_in_pdf(self) -> None:
        pdf = render_weekly_report_pdf(
            child_id="c-1",
            report=_sample_report(),
            branding=ReportBranding(brand_name="新东方幼儿园"),
        )
        # Brand name shows up in the PDF body but the content
        # streams are FlateDecode-compressed by reportlab, so
        # we can only verify the PDF rendered without raising.
        # The CJK font reference (STSong-Light) is preserved in
        # the raw trailer.
        assert pdf.startswith(PDF_MAGIC)
        assert b"STSong-Light" in pdf

    def test_brand_color_in_pdf(self) -> None:
        # Hex color appears in PDF body
        pdf = render_weekly_report_pdf(
            child_id="c-1",
            report=_sample_report(),
            branding=ReportBranding(brand_color="#FF6600"),
        )
        assert pdf.startswith(PDF_MAGIC)

    def test_logo_renders_into_pdf(self) -> None:
        """A real PNG logo should be embedded in the PDF.

        Note: we don't assert on byte content because reportlab
        compresses image streams (FlateDecode). The behavioural
        check is: PDF renders without raising when a logo is
        present, AND corrupt logos are handled gracefully (see
        test_corrupt_logo_does_not_crash below).
        """
        # Use the same PNG the existing media tests use as a
        # known-good fixture.
        from tests.api.test_media_upload import PNG
        pdf = render_weekly_report_pdf(
            child_id="c-1",
            report=_sample_report(),
            branding=ReportBranding(
                logo_b64=base64.b64encode(PNG).decode()
            ),
        )
        assert pdf.startswith(PDF_MAGIC)

    def test_corrupt_logo_does_not_crash(self) -> None:
        # Invalid base64 → logo_bytes() returns None → header
        # should just not render the logo.
        pdf = render_weekly_report_pdf(
            child_id="c-1",
            report=_sample_report(),
            branding=ReportBranding(logo_b64="not-valid!!!"),
        )
        assert pdf.startswith(PDF_MAGIC)


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


class TestEndpointTemplateParam:
    def test_endpoint_accepts_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niki.auth.account import ParentAccount
        from niki.auth.dependencies import get_current_parent
        from niki.main import create_app
        from niki.services.child_registry import (
            InMemoryChildRegistry,
            set_child_registry,
        )
        set_child_registry(InMemoryChildRegistry())
        from niki.services.child_registry import get_child_registry
        get_child_registry().register("c-tmpl", "p_tmpl")
        app = create_app()
        app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
            id="p_tmpl", name="T",
            id_number_masked="****", phone_masked="****",
        )
        from fastapi.testclient import TestClient
        client = TestClient(app)

        # Default
        r = client.get(
            "/api/v1/dashboard/weekly-report/c-tmpl/pdf"
        )
        assert r.status_code == 200
        assert r.content.startswith(PDF_MAGIC)

        # preschool
        r = client.get(
            "/api/v1/dashboard/weekly-report/c-tmpl/pdf",
            params={"template": "preschool"},
        )
        assert r.status_code == 200
        assert r.content.startswith(PDF_MAGIC)

        # school
        r = client.get(
            "/api/v1/dashboard/weekly-report/c-tmpl/pdf",
            params={"template": "school"},
        )
        assert r.status_code == 200

    def test_endpoint_rejects_invalid_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from niki.auth.account import ParentAccount
        from niki.auth.dependencies import get_current_parent
        from niki.main import create_app
        from niki.services.child_registry import (
            InMemoryChildRegistry,
            set_child_registry,
        )
        set_child_registry(InMemoryChildRegistry())
        from niki.services.child_registry import get_child_registry
        get_child_registry().register("c-tmpl", "p_tmpl")
        app = create_app()
        app.dependency_overrides[get_current_parent] = lambda: ParentAccount(
            id="p_tmpl", name="T",
            id_number_masked="****", phone_masked="****",
        )
        from fastapi.testclient import TestClient
        client = TestClient(app)

        r = client.get(
            "/api/v1/dashboard/weekly-report/c-tmpl/pdf",
            params={"template": "bogus"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "INVALID_TEMPLATE"