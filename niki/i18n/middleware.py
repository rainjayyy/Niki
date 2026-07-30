"""Language resolution for the Niki i18n framework (BE-I18N-001).

Two entry points:

  - `pick_lang(...)`     — pure function that resolves the desired
                           language tag from the `Accept-Language`
                           header and/or a `?lang=` query param.
  - `LanguageMiddleware` — Starlette/FastAPI middleware that calls
                           `pick_lang(...)` once per request and
                           stashes the result on `request.state.lang`
                           so handlers / dependencies can read it.

The middleware is intentionally minimal: it does NOT translate
responses itself. Handlers call `get_translator().translate(key,
request.state.lang)` to render the localized `message` field.
Keeping the translation step in the handler preserves the existing
`error` CODE contract — only the `message` field becomes localized.
"""

from __future__ import annotations

from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from niki.i18n.messages import CATALOGS
from niki.i18n.translator import (
    DEFAULT_LANG,
    Translator,
    _normalize_lang_tag,
    get_translator,
)


# Hard cap on how many `Accept-Language` tags we'll bother parsing.
_MAX_AL_TAGS = 16


def pick_lang(
    accept_language: str | None = None,
    query_lang: str | None = None,
    *,
    supported: Iterable[str] | None = None,
    default: str = DEFAULT_LANG,
) -> str:
    """Resolve a language tag for the current request.

    Resolution order:
      1. `query_lang` (the `?lang=` query parameter).
      2. Highest-quality supported tag from `accept_language`.
      3. `default`.

    Args:
        accept_language: The raw `Accept-Language` header value
            (e.g. `"zh-CN,en;q=0.8"`). Optional.
        query_lang: A caller-supplied override, typically the
            `?lang=` query param. Wins over the header.
        supported: Iterable of supported language tags. Defaults
            to every catalog in `niki.i18n.messages.CATALOGS`.
        default: Language to return when nothing matches.

    Returns:
        A language tag (always non-empty).
    """
    supported_set = (
        tuple(supported) if supported is not None else tuple(CATALOGS.keys())
    )

    # 1) explicit override wins
    if query_lang:
        normalized = _normalize_lang_tag(query_lang)
        if normalized and normalized in supported_set:
            return normalized

    # 2) parse Accept-Language, picking the highest-q supported tag
    if accept_language:
        candidates = _parse_accept_language(accept_language)
        for tag in candidates:
            normalized = _normalize_lang_tag(tag)
            if normalized in supported_set:
                return normalized

    return default


def _parse_accept_language(header: str) -> list[str]:
    """Parse an `Accept-Language` header into an ordered list of tags.

    Tags are returned in *quality-descending* order. Ties preserve
    source order. Tags with malformed `q=` values are dropped.

    The result is filtered to known catalog tags (via
    `supported_set`) by the caller — this helper returns the raw
    ranking so callers can apply their own supported-language mask.
    """
    if not header:
        return []

    ranked: list[tuple[float, int, str]] = []
    for index, raw in enumerate(header.split(",")[:_MAX_AL_TAGS]):
        parts = raw.split(";")
        tag = parts[0].strip()
        if not tag:
            continue
        quality = 1.0
        for param in parts[1:]:
            param = param.strip()
            if param.startswith("q=") or param.startswith("q ="):
                try:
                    quality = float(param.split("=", 1)[1].strip())
                except ValueError:
                    quality = 0.0
                break
        # Quality 0 means "I will NOT accept this language" per RFC
        # 7231 §5.3.5; skip it.
        if quality <= 0.0:
            continue
        ranked.append((quality, index, tag))

    # Highest quality first; ties broken by source order.
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [tag for _, _, tag in ranked]


class LanguageMiddleware(BaseHTTPMiddleware):
    """Stash `request.state.lang` for every incoming request.

    Handlers can read it as:

        lang = request.state.lang
        message = get_translator().translate(key, lang)

    The middleware does NOT mutate the response — translation is the
    handler's job so the structured `error` CODE contract stays
    intact across languages.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        translator: Translator | None = None,
        default_lang: str = DEFAULT_LANG,
    ) -> None:
        super().__init__(app)
        self._translator = translator or get_translator()
        self._default_lang = default_lang
        self._supported: tuple[str, ...] = tuple(self._translator.languages)

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # `request.query_params` is cached; reading it is cheap.
        query_lang = request.query_params.get("lang")
        lang = pick_lang(
            accept_language=request.headers.get("accept-language"),
            query_lang=query_lang,
            supported=self._supported,
            default=self._default_lang,
        )
        # Stash for downstream handlers. Use a private attr name so
        # it doesn't collide with anything else that may also write
        # to `request.state`.
        request.state.lang = lang
        response = await call_next(request)
        # Echo back via Vary so downstream caches key on the
        # Accept-Language header.
        response.headers["Vary"] = "Accept-Language"
        # Surface the resolved tag as a response header for debug
        # visibility (cheap; helps frontend confirm the resolved
        # lang without parsing the body).
        response.headers["Content-Language"] = lang
        return response


__all__ = [
    "LanguageMiddleware",
    "pick_lang",
]