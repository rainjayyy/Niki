"""Minimax (MiniMax-M3) LLM client — OpenAI-compatible API.

Endpoint: {base_url}/chat/completions
Headers:
    Authorization: Bearer <MINIMAX_API_KEY>
    content-type: application/json

Body (OpenAI-compatible):
{
    "model": "MiniMax-M3",
    "messages": [{"role": "user", "content": "..."}],
    "temperature": 0.7,
    "max_tokens": 1024
}

Response (OpenAI-compatible):
{
    "id": "chatcmpl-...",
    "object": "chat.completion",
    "model": "MiniMax-M3",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": N, "completion_tokens": M, "total_tokens": N+M}
}

NOTE: This client previously targeted the Anthropic-Messages
protocol (base_url /anthropic, x-api-key header). It was
rewritten to use the OpenAI-compatible /v1 endpoint per the
actual production API surface.
"""

from typing import Any

import structlog

from niki.infrastructure.llm.base import BaseLLMClient

logger = structlog.get_logger(__name__)


class MinimaxClient(BaseLLMClient):
    """Client for Minimax via the OpenAI-compatible chat completions API.

    Used as the primary LLM for all Niki agents. Set MINIMAX_API_KEY
    in the environment (or .env) to enable real responses; otherwise
    the factory falls back to the stub.
    """

    @property
    def provider_name(self) -> str:
        return "Minimax"

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a chat completion request to Minimax.

        Args:
            messages: Chat message history. Each item is a dict
                with `role` ("system" | "user" | "assistant")
                and `content` (string).
            temperature: 0.0–2.0, higher = more creative.
            max_tokens: Response token limit.
            **kwargs: Forwarded verbatim to the upstream API
                (e.g. `top_p`, `stop`, `presence_penalty`).

        Returns:
            Parsed response dict with `content`, `model`, `usage`,
            `finish_reason` keys. Shape matched to the DeepSeek /
            doubao clients so the agent layer is provider-agnostic.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        logger.debug(
            "minimax.chat.request",
            model=self.model,
            message_count=len(messages),
            temperature=temperature,
        )

        response = await self.client.post("/chat/completions", json=payload)
        self._handle_http_error(response.status_code, self.provider_name)
        response.raise_for_status()

        data = response.json()
        choice = data["choices"][0]

        result: dict[str, Any] = {
            "content": choice["message"]["content"],
            "model": data.get("model", self.model),
            "usage": data.get("usage", {}),
            "finish_reason": choice.get("finish_reason", "stop"),
        }
        # Pass through tool_calls when present (OpenAI standard; MiniMax-M3
        # supports native tool_use). The request-side `tools=[...]` and
        # `tool_choice=...` are already forwarded via **kwargs.
        if choice["message"].get("tool_calls"):
            result["tool_calls"] = choice["message"]["tool_calls"]

        logger.debug(
            "minimax.chat.response",
            model=result["model"],
            finish_reason=result["finish_reason"],
            usage=result["usage"],
        )

        return result

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Any:
        """Stream a chat completion response from Minimax (OpenAI SSE).

        POSTs `/chat/completions` with `"stream": true` and returns
        the raw httpx streaming response. The caller iterates the
        SSE chunks; format is OpenAI-standard (data: {...}\n\n).
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **kwargs,
        }

        logger.debug("minimax.chat.stream.start", model=self.model)

        response = await self.client.send(
            method="POST",
            url="/chat/completions",
            json=payload,
            stream=True,
        )
        self._handle_http_error(response.status_code, self.provider_name)
        response.raise_for_status()

        return response
