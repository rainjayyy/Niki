"""Tests for the BE-I18N-001 in-house i18n framework."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from niki.i18n import (
    DEFAULT_LANG,
    Translator,
    get_translator,
    reset_translator,
    set_translator,
    t,
)
from niki.i18n.messages import (
    CATALOGS,
    MESSAGES_EN,
    MESSAGES_ZH,
    known_languages,
)
from niki.i18n.middleware import (
    LanguageMiddleware,
    pick_lang,
    _parse_accept_language,
)
from niki.i18n.translator import FALLBACK_LANG, _normalize_lang_tag


# ---------------------------------------------------------------------------
# Translator core
# ---------------------------------------------------------------------------


def test_translate_zh_returns_chinese_string() -> None:
    """`translate("auth.invalid_phone", "zh")` returns the Chinese string."""
    translator = Translator()
    result = translator.translate("auth.invalid_phone_format", "zh")
    assert result == MESSAGES_ZH["auth.invalid_phone_format"]
    # Sanity: result is non-empty and not the key itself.
    assert result and result != "auth.invalid_phone_format"


def test_translate_en_returns_english_string() -> None:
    """`translate("auth.invalid_phone", "en")` returns the English string."""
    translator = Translator()
    result = translator.translate("auth.invalid_phone_format", "en")
    assert result == MESSAGES_EN["auth.invalid_phone_format"]


def test_translate_unknown_lang_falls_back_to_english() -> None:
    """`translate("auth.invalid_phone", "fr")` falls back to English."""
    translator = Translator()
    result = translator.translate("auth.invalid_phone_format", "fr")
    assert result == MESSAGES_EN["auth.invalid_phone_format"]


def test_translate_unknown_key_returns_key_itself() -> None:
    """`translate("auth.nonexistent_key", "en")` returns the key."""
    translator = Translator()
    result = translator.translate("auth.nonexistent_key", "en")
    assert result == "auth.nonexistent_key"


def test_translate_unknown_key_unknown_lang_returns_key() -> None:
    """Unknown key + unknown lang still returns the key."""
    translator = Translator()
    result = translator.translate("does.not.exist", "fr")
    assert result == "does.not.exist"


def test_translate_with_explicit_default_overrides_key_fallback() -> None:
    translator = Translator()
    result = translator.translate(
        "missing.key", "zh", default="我自己的兜底文案"
    )
    assert result == "我自己的兜底文案"


def test_default_lang_is_chinese() -> None:
    """The product default is Chinese — this is a Chinese product."""
    assert DEFAULT_LANG == "zh"
    translator = Translator()
    assert translator.default_lang == "zh"
    assert translator.fallback_lang == "en"


def test_known_languages_includes_zh_and_en() -> None:
    langs = known_languages()
    assert "zh" in langs
    assert "en" in langs


def test_every_zh_key_has_en_counterpart() -> None:
    """No translation drift: every key in `MESSAGES_ZH` exists in `MESSAGES_EN`.

    During the rollout we may legitimately have partial en coverage
    — but for the BE-I18N-001 baseline we ship both catalogs
    complete. This test guards against adding a key to one catalog
    and forgetting the other.
    """
    missing = sorted(set(MESSAGES_ZH) - set(MESSAGES_EN))
    assert missing == [], f"en catalog missing keys: {missing}"


def test_translate_many() -> None:
    translator = Translator()
    out = translator.translate_many(
        ["auth.invalid_phone_format", "media.file_not_found"],
        lang="zh",
    )
    assert out["auth.invalid_phone_format"] == MESSAGES_ZH["auth.invalid_phone_format"]
    assert out["media.file_not_found"] == MESSAGES_ZH["media.file_not_found"]


def test_module_level_shorthand_t() -> None:
    """`t(key)` resolves via the process-wide singleton."""
    # Reset to a fresh default to avoid pollution from prior tests.
    reset_translator()
    try:
        result = t("auth.invalid_phone_format", "zh")
        assert result == MESSAGES_ZH["auth.invalid_phone_format"]
    finally:
        reset_translator()


def test_set_translator_overrides_singleton() -> None:
    custom = Translator(
        catalogs={
            "zh": {"only.key": "只有中文"},
            "en": {"only.key": "English only"},
        }
    )
    set_translator(custom)
    try:
        assert get_translator() is custom
        assert t("only.key", "en") == "English only"
    finally:
        set_translator(None)
        reset_translator()


def test_normalize_lang_tag_strips_region() -> None:
    assert _normalize_lang_tag("zh-CN") == "zh"
    assert _normalize_lang_tag("en-US") == "en"
    assert _normalize_lang_tag("fr-CA") == "fr"


def test_normalize_lang_tag_handles_q_suffix() -> None:
    assert _normalize_lang_tag("zh-CN;q=0.9") == "zh"
    assert _normalize_lang_tag("en;q=0.5") == "en"


# ---------------------------------------------------------------------------
# pick_lang
# ---------------------------------------------------------------------------


def test_pick_lang_accept_language_zh_cn_prefers_zh() -> None:
    assert pick_lang(accept_language="zh-CN,en;q=0.8") == "zh"


def test_pick_lang_accept_language_en_us_returns_en() -> None:
    assert pick_lang(accept_language="en-US") == "en"


def test_pick_lang_defaults_to_zh_when_no_input() -> None:
    assert pick_lang() == "zh"


def test_pick_lang_default_is_zh_not_en() -> None:
    """Product is Chinese-first; default is `zh`, not `en`."""
    assert pick_lang(accept_language=None, query_lang=None) == "zh"


def test_pick_lang_query_overrides_header() -> None:
    """`?lang=en` beats the Accept-Language header (en is supported)."""
    assert pick_lang(
        accept_language="zh-CN", query_lang="en"
    ) == "en"


def test_pick_lang_unsupported_query_falls_back_to_header() -> None:
    """Unsupported `?lang=` does NOT replace the Accept-Language match."""
    # Pick a tag that isn't in our catalogs.
    assert pick_lang(
        accept_language="zh-CN", query_lang="fr"
    ) == "zh"


def test_pick_lang_quality_ordering() -> None:
    """Higher-q tag wins even if listed second."""
    assert pick_lang(accept_language="en;q=0.1,zh;q=0.9") == "zh"
    assert pick_lang(accept_language="zh;q=0.1,en;q=0.9") == "en"


def test_pick_lang_skips_unsupported_then_returns_default() -> None:
    """All-AL tags unsupported → default."""
    assert pick_lang(accept_language="fr,de;q=0.9") == "zh"


def test_pick_lang_handles_empty_header() -> None:
    assert pick_lang(accept_language="") == "zh"


def test_parse_accept_language_drops_zero_quality() -> None:
    """`q=0` tags should not be selected."""
    out = _parse_accept_language("zh;q=0,en;q=1")
    assert out[0] == "en"


# ---------------------------------------------------------------------------
# LanguageMiddleware via FastAPI
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LanguageMiddleware)

    @app.get("/echo-lang")
    def echo_lang(request: Request) -> dict:  # type: ignore[no-untyped-def]
        return {"lang": request.state.lang}

    return app


def test_middleware_uses_accept_language_header() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo-lang", headers={"Accept-Language": "zh-CN,en;q=0.8"})
    assert r.status_code == 200
    assert r.json() == {"lang": "zh"}
    assert r.headers["Content-Language"] == "zh"
    # `Vary: Accept-Language` so downstream caches key on the header.
    assert "Accept-Language" in r.headers.get("Vary", "")


def test_middleware_uses_en_header() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo-lang", headers={"Accept-Language": "en-US"})
    assert r.status_code == 200
    assert r.json() == {"lang": "en"}


def test_middleware_query_param_overrides_header() -> None:
    client = TestClient(_build_app())
    r = client.get(
        "/echo-lang?lang=en",
        headers={"Accept-Language": "zh-CN"},
    )
    assert r.json() == {"lang": "en"}


def test_middleware_default_when_no_header_no_query() -> None:
    client = TestClient(_build_app())
    r = client.get("/echo-lang")
    assert r.json() == {"lang": "zh"}
    assert r.headers["Content-Language"] == "zh"


def test_middleware_unsupported_lang_falls_back() -> None:
    client = TestClient(_build_app())
    r = client.get(
        "/echo-lang",
        headers={"Accept-Language": "klingon"},
    )
    # klingon isn't in our catalogs → default zh.
    assert r.json() == {"lang": "zh"}


# ---------------------------------------------------------------------------
# Catalog coverage of the refactored high-traffic codes
# ---------------------------------------------------------------------------


def test_auth_invalid_phone_format_is_in_both_catalogs() -> None:
    assert "auth.invalid_phone_format" in MESSAGES_ZH
    assert "auth.invalid_phone_format" in MESSAGES_EN


def test_media_file_not_found_is_in_both_catalogs() -> None:
    assert "media.file_not_found" in MESSAGES_ZH
    assert "media.file_not_found" in MESSAGES_EN


def test_parent_child_mismatch_is_in_both_catalogs() -> None:
    assert "auth.parent_child_mismatch" in MESSAGES_ZH
    assert "auth.parent_child_mismatch" in MESSAGES_EN


def test_invalid_template_is_in_both_catalogs() -> None:
    assert "dashboard.invalid_template" in MESSAGES_ZH
    assert "dashboard.invalid_template" in MESSAGES_EN


def test_invalid_sort_is_in_both_catalogs() -> None:
    # auth_audit.invalid_sort + dashboard.invalid_sort
    assert "auth_audit.invalid_sort" in MESSAGES_ZH
    assert "auth_audit.invalid_sort" in MESSAGES_EN


def test_catalogs_dict_lists_known_languages() -> None:
    assert set(CATALOGS.keys()) == set(known_languages())