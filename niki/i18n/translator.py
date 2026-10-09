"""Translator class for the Niki i18n framework (BE-I18N-001).

The `Translator` resolves a `(key, lang)` pair to a localized
string. Lookup order:

    1. `lang` catalog (if the language is known)
    2. fallback language (English by default)
    3. the literal key itself

A process-wide singleton is exposed via `get_translator()` /
`set_translator()`. Tests may swap the catalog via `set_translator`
to assert specific translations without touching the global state
of the whole test suite.
"""

from __future__ import annotations

from threading import Lock
from typing import Iterable

from niki.i18n.messages import CATALOGS, MESSAGES_EN, MESSAGES_ZH


# Chinese-first product: `zh` is the canonical default.
DEFAULT_LANG = "zh"

# Languages we never fall back from (i.e. always trusted). When the
# requested language is missing we fall back to this, then to the key.
FALLBACK_LANG = "en"


def _normalize_lang_tag(tag: str) -> str:
    """Lower-case + strip the region qualifier (`zh-CN` -> `zh`).

    HTTP `Accept-Language` headers carry tags like `zh-CN` or
    `en-US`. Our catalogs are keyed by the base language only
    (`zh`, `en`), so we strip the optional region suffix.
    """
    if not tag:
        return ""
    tag = tag.strip().lower()
    if not tag:
        return ""
    # `;q=0.8` quality suffix used by some clients; drop it.
    if ";" in tag:
        tag = tag.split(";", 1)[0]
    if "-" in tag:
        tag = tag.split("-", 1)[0]
    return tag


class Translator:
    """Resolve dotted keys to localized strings.

    The translator is intentionally tiny — a dict lookup with two
    layers of fallback. It is safe to construct multiple instances
    (e.g. in tests) and to share one instance across threads; the
    catalog dicts are read-only after construction.
    """

    def __init__(
        self,
        catalogs: dict[str, dict[str, str]] | None = None,
        *,
        default_lang: str = DEFAULT_LANG,
        fallback_lang: str = FALLBACK_LANG,
    ) -> None:
        self._catalogs: dict[str, dict[str, str]] = (
            dict(catalogs) if catalogs is not None else dict(CATALOGS)
        )
        self._default_lang = default_lang
        self._fallback_lang = fallback_lang

    @property
    def default_lang(self) -> str:
        return self._default_lang

    @property
    def fallback_lang(self) -> str:
        return self._fallback_lang

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._catalogs.keys())

    def has_key(self, key: str) -> bool:
        """True iff `key` exists in the default catalog."""
        default = self._catalogs.get(self._default_lang, {})
        return key in default

    def translate(
        self,
        key: str,
        lang: str | None = None,
        *,
        default: str | None = None,
    ) -> str:
        """Resolve `key` for `lang`.

        Lookup order:
          1. `lang` catalog (if known and the key is present)
          2. fallback language catalog
          3. `default` if provided
          4. the literal `key` itself (never raises)

        An empty/unknown `lang` is silently treated as the default.
        """
        lang_tag = _normalize_lang_tag(lang or self._default_lang)
        catalog = self._catalogs.get(lang_tag)
        if catalog is not None:
            value = catalog.get(key)
            if value is not None:
                return value

        # First fallback: fallback language (English by default).
        if lang_tag != self._fallback_lang:
            fallback_catalog = self._catalogs.get(self._fallback_lang)
            if fallback_catalog is not None:
                value = fallback_catalog.get(key)
                if value is not None:
                    return value

        # Second fallback: caller-provided default.
        if default is not None:
            return default

        # Final fallback: return the key so the caller can still
        # log / surface something useful.
        return key

    def translate_many(
        self,
        keys: Iterable[str],
        lang: str | None = None,
    ) -> dict[str, str]:
        """Bulk-translate. Returns a dict {key: translated}."""
        return {key: self.translate(key, lang) for key in keys}


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_TRANSLATOR: Translator | None = None
_LOCK = Lock()


def get_translator() -> Translator:
    """Return the process-wide `Translator` singleton.

    Created lazily so importing this module has zero side-effects.
    """
    global _TRANSLATOR
    if _TRANSLATOR is None:
        with _LOCK:
            if _TRANSLATOR is None:
                _TRANSLATOR = Translator()
    return _TRANSLATOR


def set_translator(translator: Translator | None) -> None:
    """Override the process-wide translator (tests / runtime swap).

    Pass `None` to reset back to a freshly-constructed default on
    the next `get_translator()` call.
    """
    global _TRANSLATOR
    _TRANSLATOR = translator


def reset_translator() -> None:
    """Force the next `get_translator()` call to rebuild from defaults.

    Mostly a test helper — preferred over passing a `None` through
    `set_translator` because it's explicit about intent.
    """
    global _TRANSLATOR
    _TRANSLATOR = None


# ---------------------------------------------------------------------------
# Convenience helper
# ---------------------------------------------------------------------------


def t(key: str, lang: str | None = None) -> str:
    """Module-level shorthand for `get_translator().translate(key, lang)`."""
    return get_translator().translate(key, lang)


__all__ = [
    "DEFAULT_LANG",
    "FALLBACK_LANG",
    "Translator",
    "get_translator",
    "reset_translator",
    "set_translator",
    "t",
    "MESSAGES_ZH",
    "MESSAGES_EN",
]
