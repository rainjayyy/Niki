"""Minimax (MiniMax-M3) LLM client — Anthropic-Messages-compatible API.

Endpoint: {base_url}/v1/messages
Headers:
    x-api-key: <MINIMAX_API_KEY>
    anthropic-version: 2023-06-01
    content-type: application/json

Body:
{
    "model": "MiniMax-M3",
    "max_tokens": 1024,
    "system": "<system prompt>",         // separate from messages
    "messages": [{"role": "user", "content": "..."}],
    "temperature": 0.7
}

Response:
{
    "id": "msg_...",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "..."}],
    "model": "MiniMax-M3",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": N, "output_tokens": M}
}
"""

from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient

logger = structlog.get_logger(__name__)

# Anthropic-Messages API version header. Pinned to the spec the server
# advertises; bump when the upstream supports a newer protocol revision.
ANTHROPIC_API_VERSION = "2023-06-01"


class MinimaxClient(BaseLLMClient):
    """Client for Minimax via the Anthropic-Messages-compatible API.

    Minimax speaks the Anthropic /v1/messages protocol with two
    transport-level differences from the canonical Anthropic endpoint:
      1. base_url is `https://api.minimaxi.com/anthropic` (no /v1).
      2. The model name is configured via `settings.minimax_model`
         (defaults to "MiniMax-M3" but the server may accept other
         identifiers).

    The request / response payload shape is otherwise identical to
    Anthropic's Messages API, so we share the conversation flow with
    any other Anthropic-compatible provider (e.g. z.ai, Yi).
    """

    @property
    def provider_name(self) -> str:
        return "Minimax"

    def _default_headers(self) -> dict[str, str]:
        """Anthropic-Messages auth: x-api-key + anthropic-version.

        Note: Minimax does NOT accept the `Authorization: Bearer ...`
        header that OpenAI-compatible APIs use.
        """
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request to Minimax.

        Args:
            messages: Chat message history. Each item is a dict with
                `role` ("user" | "assistant") and `content` (string).
            temperature: 0.0–1.0 for Anthropic-compatible APIs.
            max_tokens: Response token limit (Anthropic requires this).
            system: Optional system prompt (Anthropic separates system
                from the messages array; if absent, falls back to the
                first system-role message in `messages` if present).
            **kwargs: Forwarded verbatim to the upstream API (e.g.
                `top_p`, `stop_sequences`).

        Returns:
            Parsed response dict with `content`, `model`, `usage`,
            `finish_reason` keys — shape matched to the DeepSeek /
            doubao clients so the agent layer is provider-agnostic.
        """
        # Extract system prompt if it leaked into the messages array
        # (some callers pass `{"role": "system", ...}` Anthropic-style).
        normalized: list[dict[str, str]] = []
        for m in messages:
            if m.get("role") == "system":
                if system is None:
                    system = m.get("content", "")
                # Drop it — Anthropic takes system out-of-band.
                continue
            normalized.append({"role": m["role"], "content": m["content"]})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": normalized,
            "temperature": temperature,
            **kwargs,
        }
        if system:
            payload["system"] = system

        logger.debug(
            "minimax.chat.request",
            model=self.model,
            message_count=len(normalized),
            temperature=temperature,
            has_system=bool(system),
        )

        response = await self.client.post("/v1/messages", json=payload)
        self._handle_http_error(response.status_code, self.provider_name)
        response.raise_for_status()

        data = response.json()

        # Anthropic-Messages response shape:
        # content is a list of blocks; we concatenate text blocks.
        blocks = data.get("content", [])
        text = "".join(
            block.get("text", "")
            for block in blocks
            if block.get("type") == "text"
        )

        # Map Anthropic stop_reason → OpenAI-style finish_reason so the
        # agent layer doesn't need provider-specific logic.
        stop_reason = data.get("stop_reason", "end_turn")
        finish_reason = {
            "end_turn": "stop",
            "max_tokens": "length",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
        }.get(stop_reason, "stop")

        result: dict[str, Any] = {
            "content": text,
            "model": data.get("model", self.model),
            "usage": data.get("usage", {}),
            "finish_reason": finish_reason,
            "raw_finish_reason": stop_reason,
        }

        logger.debug(
            "minimax.chat.response",
            model=result["model"],
            finish_reason=result["finish_reason"],
            usage=result["usage"],
        )

        return result

    async def stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        system: str | None = None,
        **kwargs: Any,
    ):
        """Stream a chat completion from Minimax (Anthropic SSE).

        POSTs `/v1/messages` with `"stream": true` and yields the
        incremental `content_block_delta.delta.text` chunks as they
        arrive, so the SSE wrapper can forward them live.

        The upstream emits `text/event-stream` lines:
            event: content_block_delta
            data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}

        Yields:
            Successive plain-text chunks (no SSE framing — the
            endpoint adds the `data: ` wrapper).
        """
        # Same normalization as `chat()`: extract system prompt from
        # the messages array if the caller put it there.
        normalized: list[dict[str, str]] = []
        for m in messages:
            if m.get("role") == "system":
                if system is None:
                    system = m.get("content", "")
                continue
            normalized.append({"role": m["role"], "content": m["content"]})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": normalized,
            "temperature": temperature,
            "stream": True,
            **kwargs,
        }
        if system:
            payload["system"] = system

        logger.debug(
            "minimax.stream.start",
            model=self.model,
            message_count=len(normalized),
        )

        async with self.client.stream(
            "POST", "/v1/messages", json=payload
        ) as response:
            self._handle_http_error(response.status_code, self.provider_name)
            async for line in response.aiter_lines():
                line = line.strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                import json as _json

                try:
                    event = _json.loads(data_str)
                except _json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype != "content_block_delta":
                    continue
                delta = event.get("delta") or {}
                if delta.get("type") != "text_delta":
                    continue
                text = delta.get("text") or ""
                if text:
                    yield text