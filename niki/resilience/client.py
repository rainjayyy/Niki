"""Circuit-breaker-aware LLM client — PRD #58, #59.

Wraps a primary (DeepSeek-V4) and fallback (doubao) LLM client.
On every call:
1. Check the breaker
2. If CLOSED → call primary
3. If OPEN   → fall back to doubao (PRD: still safe + functional)
4. If HALF_OPEN → one probe to primary; others use doubao

Failure handling:
- Primary call fails → record_failure(breaker) + return doubao result
  (PRD #59: safety, intent-routing, persona still work)
- Primary call succeeds → record_success(breaker)
- Probe succeeds → close breaker
- Probe fails → reopen breaker with doubled cooldown
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from niki.resilience.breaker import (
    CircuitBreaker,
)
from niki.resilience.notifier import DegradationNotifier

logger = structlog.get_logger(__name__)


@dataclass
class CircuitBreakerAwareClient:
    """A drop-in replacement for an LLM client that uses the breaker.

    Usage:
        primary = DeepSeekClient(...)
        fallback = DoubaoClient(...)
        breaker = CircuitBreaker()
        notifier = DegradationNotifier()

        client = CircuitBreakerAwareClient(
            primary=primary, fallback=fallback,
            breaker=breaker, notifier=notifier,
        )
        result = await client.chat(messages=...)
    """

    primary: Any                       # DeepSeek-V4 (or stub)
    fallback: Any                      # doubao (or stub)
    breaker: CircuitBreaker
    notifier: DegradationNotifier | None = None
    primary_name: str = "deepseek-v4"
    fallback_name: str = "doubao"

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make an LLM call, with circuit-breaker protection.

        Returns:
            The LLM response dict (provider-specific).

        Always succeeds: even when primary fails AND fallback fails,
        a last-resort stub response is returned.
        """
        decision = await self.breaker.before_call()

        if not decision.allowed:
            # Fail fast → fallback (PRD: degraded but functional)
            logger.info(
                "client.using_fallback",
                reason=decision.reason,
                state=decision.state.value,
            )
            await self._notify_degradation(decision.reason)
            return await self._call_fallback(messages, temperature, max_tokens, **kwargs)

        if decision.is_probe:
            # Half-open probe: try primary, treat result with care
            try:
                result = await self._call_primary(
                    messages, temperature, max_tokens, **kwargs
                )
                await self.breaker.record_success()
                logger.info("client.probe_succeeded")
                return result
            except Exception as exc:
                await self.breaker.record_failure(str(exc))
                logger.warning("client.probe_failed", error=str(exc))
                await self._notify_degradation(f"probe failed: {exc}")
                return await self._call_fallback(
                    messages, temperature, max_tokens, **kwargs
                )

        # CLOSED: try primary
        try:
            result = await self._call_primary(
                messages, temperature, max_tokens, **kwargs
            )
            await self.breaker.record_success()
            return result
        except Exception as exc:
            await self.breaker.record_failure(str(exc))
            logger.warning(
                "client.primary_failed_using_fallback",
                error=str(exc),
            )
            await self._notify_degradation(f"primary failed: {exc}")
            return await self._call_fallback(
                messages, temperature, max_tokens, **kwargs
            )

    async def _call_primary(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call the primary (DeepSeek-V4) client."""
        return await self.primary.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def _call_fallback(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Call the fallback (doubao) client.

        If the fallback also fails, return a last-resort stub response
        (PRD: Niki must always be available).
        """
        try:
            return await self.fallback.chat(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:
            logger.error("client.fallback_also_failed", error=str(exc))
            # Last-resort: return a safe stub
            return {
                "content": "嗯~ Niki现在有点累了，稍等一下~",
                "model": "last-resort-stub",
                "usage": {},
                "finish_reason": "fallback",
            }

    async def _notify_degradation(self, reason: str) -> None:
        """Send silent signal to parent dashboard (PRD: 静默感知)."""
        if self.notifier is not None:
            await self.notifier.notify_active(reason)
