"""LLM client factory — picks the real provider based on settings, falls back.

Design:
- When MINIMAX_API_KEY (or the configured primary provider's key) is set,
  the factory returns a real `MinimaxClient` (or whatever provider the
  `primary_llm_provider` setting names).
- When no key is configured, returns a `FallbackLLM` so the API still
  boots for local dev / tests / smoke checks.
- `get_or_create_llm()` is a process-wide singleton; tests that need
  isolation use `reset_for_tests()`.

The endpoint modules call `get_or_create_llm()` at import time and
pass the result to each agent. Switching provider = restart process.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog

from niki.config.settings import Settings, get_settings
from niki.infrastructure.llm.base import BaseLLMClient
from niki.infrastructure.llm.deepseek import DeepSeekClient
from niki.infrastructure.llm.doubao import DoubaoClient
from niki.infrastructure.llm.minimax import MinimaxClient

logger = structlog.get_logger(__name__)

# Sentinel values from .env templates that should NOT be treated as real
# credentials. Substring match is case-insensitive and checks for the
# typical placeholder tokens shipped in `docs/env.example` / `.env`.
_PLACEHOLDER_TOKENS = (
    "your-",
    "your_",
    "changeme",
    "change-me",
    "xxx",
    "sk-your-",
    "sk-placeholder",
)


def _is_real_key(value: str) -> bool:
    """A key counts as real iff it's non-empty and not a placeholder."""
    if not value:
        return False
    stripped = value.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    return not any(token in lowered for token in _PLACEHOLDER_TOKENS)


_lock = threading.Lock()
_singleton: BaseLLMClient | None = None


class FallbackLLM(BaseLLMClient):
    """Trivial stub used when no real API key is configured.

    Returns a deterministic, safe response. Mirrors the
    `niki.api._fallback_llm.FallbackLLM` shape so agent code doesn't need
    to special-case the absence of credentials.
    """

    @property
    def provider_name(self) -> str:
        return "stub"

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "content": "嗯~ Niki在听你说呢~",
            "model": "stub-llm",
            "usage": {"total_tokens": 0},
            "finish_reason": "stop",
        }


def _build_client(settings: Settings) -> BaseLLMClient:
    """Pick a real client based on the `primary_llm_provider` setting.

    Resolution order (each provider checked only if its API key is set):
        1. settings.primary_llm_provider (default: "minimax")
        2. If the named provider has no real key, walk through the rest
        3. If nothing has a real key, return the stub
    """
    candidates: list[tuple[str, BaseLLMClient | None]] = [
        (
            "minimax",
            MinimaxClient(
                api_key=settings.minimax_api_key,
                base_url=settings.minimax_base_url,
                model=settings.minimax_model,
                timeout=90,  # raised from settings default 30 — too short for reasoning models
                max_retries=settings.minimax_max_retries,
            )
            if _is_real_key(settings.minimax_api_key)
            else None,
        ),
        (
            "deepseek",
            DeepSeekClient(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                timeout=settings.deepseek_timeout,
                max_retries=settings.deepseek_max_retries,
            )
            if _is_real_key(settings.deepseek_api_key)
            else None,
        ),
        (
            "doubao",
            DoubaoClient(
                api_key=settings.doubao_api_key,
                base_url=settings.doubao_base_url,
                model=settings.doubao_model,
                timeout=settings.doubao_timeout,
                max_retries=settings.doubao_max_retries,
            )
            if _is_real_key(settings.doubao_api_key)
            else None,
        ),
    ]

    preferred = settings.primary_llm_provider
    # Try preferred first
    for name, client in candidates:
        if name == preferred and client is not None:
            logger.info(
                "llm.factory.selected",
                provider=name,
                model=client.model,
            )
            return client
    # Then any available
    for name, client in candidates:
        if client is not None:
            logger.warning(
                "llm.factory.preferred_missing",
                preferred=preferred,
                using=name,
            )
            return client

    logger.warning(
        "llm.factory.no_credentials",
        note=(
            "All LLM providers have empty / placeholder API keys. The "
            "API will return stub responses. Set MINIMAX_API_KEY (or "
            "DEEPSEEK_API_KEY / DOUBAO_API_KEY) in the environment to "
            "enable real responses."
        ),
    )
    return FallbackLLM(
        api_key="",
        base_url="",
        model="stub-llm",
        timeout=1,
        max_retries=0,
    )


def get_or_create_llm() -> BaseLLMClient:
    """Process-wide singleton LLM client.

    The factory is called once per process and cached. To pick up
    setting changes (e.g. after rotating keys) restart the process
    or call `reset_for_tests()` followed by `get_or_create_llm()`.
    """
    global _singleton
    if _singleton is None:
        with _lock:
            if _singleton is None:
                _singleton = _build_client(get_settings())
    return _singleton


def is_fallback_mode() -> bool:
    """True when no real provider is wired — i.e. the factory returned
    the fallback stub. Endpoint modules use this to decide whether
    they should layer on a more domain-aware stub (e.g. the
    encyclopedia-specific KB-echo stub) on top of the generic stub.
    """
    client = get_or_create_llm()
    return getattr(client, "provider_name", "") == "stub"


def reset_for_tests() -> None:
    """Drop the cached singleton so the next call rebuilds from settings.

    Used by `tests/conftest.py` to isolate per-test LLM configuration
    without restarting the process.
    """
    global _singleton
    with _lock:
        old = _singleton
        _singleton = None
    # Best-effort cleanup of the old client's httpx connection pool.
    if old is not None:
        try:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None and loop.is_running():
                loop.create_task(old.close())
            else:
                cleanup_loop = asyncio.new_event_loop()
                try:
                    cleanup_loop.run_until_complete(old.close())
                finally:
                    cleanup_loop.close()
        except Exception:  # pragma: no cover — best effort
            pass
