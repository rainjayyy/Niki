"""Small utilities for the bounded ReAct tool-use loop (ISS-22).

No business logic — pure helpers used by EncyclopediaAgent's loop:

  * ``estimate_tokens`` — cheap char-based token estimate.
  * ``truncate_messages`` — drop-oldest sliding-window truncator.
  * ``detect_repeat`` — return True when an LLM is looping on the same
    tool call with the same arguments.

These exist as a separate module (not nested in encyclopedia.py) so the
helpers stay unit-testable in isolation and reusable when ISS-23 adds
the streaming variant.
"""

from __future__ import annotations

from typing import Any


# 4 chars per token is a deliberately conservative estimate: English
# text averages 4 chars/token; CJK averages ~1.5 chars/token. We err on
# the safe (slightly inflated) side so the truncation gate fires
# sooner rather than later. False positives here just mean we drop an
# older round a bit early; the alternative — sending too many tokens
# to the LLM — is worse (cost + latency + occasional provider 400).
_CHARS_PER_TOKEN = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Return a conservative token estimate for ``messages``.

    Walks every message's ``content`` (str) plus the string form of
    ``tool_calls[].function.arguments`` (which carries the KB query).
    Returns the integer ceil of total_chars / 4 — never 0 unless
    ``messages`` is empty.

    No tiktoken dep — the project has never counted tokens precisely;
    this is greenfield. Used by the encyclopedia loop as a SOFT gate:
    if the estimate exceeds the per-call budget, we truncate before
    invoking the LLM again, not because we know the exact count.
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                total_chars += len(args)
    return (total_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def truncate_messages(
    messages: list[dict[str, Any]],
    soft_cap_chars: int,
    *,
    preserve_first_user_index: int = 2,
) -> list[dict[str, Any]]:
    """Drop oldest complete round(s) until total char-count fits ``soft_cap_chars``.

    Preserves (always):

      * the system message at ``messages[0]``
      * the first user message at ``messages[preserve_first_user_index]``
        (default 2 — matches the loop's initial layout
        ``[system, *history, user]``).
      * the LAST assistant + tool-call block, since OpenAI spec requires
        the assistant message that issued the tool_calls to immediately
        precede the role:tool messages (BUG-1 from 2026-10-09).

    A "complete round" is one assistant message that carries
    ``tool_calls`` plus the immediately following role:tool messages.
    We drop one round at a time so the truncation is predictable and
    safe under repeated calls (mirrors the drop-oldest sliding-window
    precedent in ``niki/memory/short_term.py:84-89``).

    Args:
        messages: The current OpenAI-style message list.
        soft_cap_chars: After truncation, total chars across all
            messages must be at or below this number. Picked to be
            ``max_tokens * 4`` so the estimate gate never trips
            immediately after truncation.

    Returns:
        A new list. Original ``messages`` is never mutated.
    """
    # Fast path — already within budget.
    total = _total_chars(messages)
    if total <= soft_cap_chars:
        return list(messages)

    # Find the lower bound of "droppable" content. Always keep:
    #   - messages[0] = system prompt
    #   - messages[preserve_first_user_index] = first user question
    #   - the LAST assistant+tool block at the tail
    keep_until = max(1, preserve_first_user_index + 1)
    # Identify the last assistant message with tool_calls (the tail of
    # the conversation). If none exists, we keep everything from
    # ``keep_until`` onward.
    last_assistant_with_tools = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            last_assistant_with_tools = idx
            break
    if last_assistant_with_tools > keep_until:
        keep_until = last_assistant_with_tools

    # Build the kept prefix = [system, ...up to keep_until).
    kept_prefix = list(messages[:keep_until])
    # Tail = everything after the kept prefix.
    tail = list(messages[keep_until:])

    # Drop oldest complete round from ``tail`` while over budget.
    while _total_chars(kept_prefix + tail) > soft_cap_chars and tail:
        tail = _drop_oldest_round(tail)

    return kept_prefix + tail


def _total_chars(messages: list[dict[str, Any]]) -> int:
    """Sum of (str-content length + tool-call args length) over all messages."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += len(content)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                total += len(args)
    return total


def _drop_oldest_round(tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop one complete round from the front of ``tail``.

    A round = the first assistant message with ``tool_calls`` plus the
    contiguous role:tool messages that follow it. If the first message
    isn't an assistant-with-tool-calls (e.g. it was just a plain
    ``assistant`` or ``user``), we drop just that single message.
    """
    if not tail:
        return tail
    head = tail[0]
    if head.get("role") == "assistant" and head.get("tool_calls"):
        # Find end of the contiguous role:tool block.
        end = 1
        while end < len(tail) and tail[end].get("role") == "tool":
            end += 1
        return tail[end:]
    # Not a tool round — drop just the first message.
    return tail[1:]


def detect_repeat(
    tool_call_history: list[tuple[str, str]],
    new_tool_call: tuple[str, str],
) -> bool:
    """Return True if ``new_tool_call`` already appears in ``tool_call_history``.

    ``tool_call_history`` is a list of ``(tool_name, args_str)`` tuples
    accumulated across the loop's rounds. ``args_str`` is the raw
    JSON-serialized ``function.arguments`` from the LLM; we compare
    strings (cheaper, no JSON round-trip per call) and rely on the LLM
    to emit stable JSON for identical intent.

    Used by the encyclopedia loop to break infinite "LLM keeps calling
    KB with the same query" patterns. False positives are acceptable
    (we'd just stop one round early); false negatives risk runaway
    cost — we err on the side of detection.
    """
    return new_tool_call in tool_call_history
