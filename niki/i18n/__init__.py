"""Niki i18n framework (BE-I18N-001).

This package provides a small in-house translation framework:

    from niki.i18n import get_translator, t
    translator = get_translator()
    msg = translator.translate("auth.invalid_phone", lang="zh")

The framework has three modules:

  - `messages`    — plain-Python dict catalogs keyed by dotted key
                   (`auth.invalid_phone`, `media.file_not_found`).
                   Currently ships `zh` and `en`.
  - `translator` — `Translator` class with a `translate(key, lang)`
                   lookup that falls back to English and then to
                   the key itself if missing.
  - `middleware` — `LanguageMiddleware` + `pick_lang(...)` helper
                   that reads the `Accept-Language` header (and
                   optional `?lang=` query param) and stashes the
                   resolved language on `request.state.lang`.

No external dependencies (no `babel`, no `gettext`).
"""

from niki.i18n.translator import (
    DEFAULT_LANG,
    Translator,
    get_translator,
    reset_translator,
    set_translator,
    t,
)

__all__ = [
    "DEFAULT_LANG",
    "Translator",
    "get_translator",
    "reset_translator",
    "set_translator",
    "t",
]
