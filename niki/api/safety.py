"""Safety API endpoints — child-facing chat with input/output gateways.

This module demonstrates end-to-end safety integration. It provides
a /chat endpoint that runs the input gateway before any LLM call, then
runs the output gateway before returning the response to the caller.

In production, this endpoint would dispatch to the actual LLM (DeepSeek-V4
or doubao). For P0+#1 demonstration, the response is mocked so the safety
pipeline can be exercised and tested without API keys.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from niki.safety.input_gateway import InputSafetyGateway
from niki.safety.output_gateway import OutputAction, OutputSafetyGateway, OutputVerdict

router = APIRouter(
    prefix="/safety",
    tags=[
        {
            "name": "safety",
            "description": ("Safety API endpoints — child-facing chat with input/output gateways."),
        },
    ],
)
chat_router = APIRouter(
    tags=[
        {
            "name": "chat",
            "description": ("Safety API endpoints — child-facing chat with input/output gateways."),
        },
    ],
)

# Singleton gateways (cheap to construct; safe to reuse)
_input_gateway = InputSafetyGateway()


def _gateway_for_age(age: int) -> OutputSafetyGateway:
    """Return an output gateway configured for a child's age."""
    return OutputSafetyGateway(child_age=age)


@router.post(
    "/check-input",
    summary="Run the input safety gateway against arbitrary text",
    description="""
            Run the input safety gateway against arbitrary text.
            
            Request body:
                {"text": "..."}
            
            Returns:
                The InputVerdict serialised to JSON.
            """,
)
async def check_input(payload: dict) -> dict:
    """Run the input safety gateway against arbitrary text.

    Request body:
        {"text": "..."}

    Returns:
        The InputVerdict serialised to JSON.
    """
    text = payload.get("text", "")
    verdict = await _input_gateway.check(text)
    return {
        "action": verdict.action.value,
        "matched_category": verdict.matched_category,
        "matched_words": verdict.matched_words,
        "topic_matches": [
            {"topic": m.topic, "matched_text": m.matched_text} for m in verdict.topic_matches
        ],
        "reason": verdict.reason,
        "is_blocked": verdict.is_blocked,
        "is_escalation": verdict.is_escalation,
        "should_downgrade": verdict.should_downgrade,
    }


@router.post(
    "/check-output",
    summary="Run the output safety gateway against arbitrary text",
    description="""
            Run the output safety gateway against arbitrary text.
            
            Request body:
                {"text": "...", "child_age": 7, "is_tutoring_response": false}
            
            Returns:
                The OutputVerdict serialised to JSON.
            """,
)
async def check_output(payload: dict) -> dict:
    """Run the output safety gateway against arbitrary text.

    Request body:
        {"text": "...", "child_age": 7, "is_tutoring_response": false}

    Returns:
        The OutputVerdict serialised to JSON.
    """
    text = payload.get("text", "")
    child_age = int(payload.get("child_age", 7))
    is_tutoring = bool(payload.get("is_tutoring_response", False))

    gateway = _gateway_for_age(child_age)
    verdict = gateway.check(text, is_tutoring_response=is_tutoring)

    return {
        "action": verdict.action.value,
        "sanitized_text": verdict.sanitized_text,
        "rewrites_applied": [
            {"from": original, "to": replacement}
            for original, replacement in verdict.rewrites_applied
        ],
        "privacy_violations": verdict.privacy_violations,
        "direct_answer_detected": verdict.direct_answer_detected,
        "sentences_over_limit": verdict.sentences_over_limit,
        "reason": verdict.reason,
        "is_safe": verdict.is_safe,
    }


@router.post(
    "/chat",
    summary="Same as /chat but routed under /safety/chat for debugging",
    description="Same as /chat but routed under /safety/chat for debugging.",
)
async def chat_under_safety(payload: dict) -> dict:
    """Same as /chat but routed under /safety/chat for debugging."""
    return await chat(payload)


@chat_router.post("/chat")
async def chat(payload: dict) -> dict:
    """End-to-end chat demonstrating full safety pipeline.

    Request body:
        {"text": "...", "child_age": 7, "mock_response": "..."}

    Behavior:
    1. Input gateway evaluates the child's text.
    2. Action dispatched: BLOCK → 400, ESCALATE → 200 with alert flag,
       DOWNGRADE_TO_EMOTION → use empathic response,
       ALLOW → use provided mock_response (LLM placeholder).
    3. Output gateway evaluates the chosen response.
    4. Returns the final text with diagnostics.

    In production, "mock_response" would be the LLM-generated reply.
    """
    text = payload.get("text", "")
    child_age = int(payload.get("child_age", 7))
    mock_response = payload.get("mock_response", "Niki听到了，我们继续聊聊吧~")
    is_tutoring = bool(payload.get("is_tutoring", False))

    if not text or not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="text is required and must be non-empty",
        )

    # ---- 1. Input gateway ----
    input_verdict = await _input_gateway.check(text)

    if input_verdict.is_blocked:
        return {
            "final_text": "嗯…这个问题Niki还没想好怎么回答呢~",
            "input_action": input_verdict.action.value,
            "input_category": input_verdict.matched_category,
            "output_action": "skipped",
            "parent_alert_required": False,
        }

    # ---- 2. Pick response based on input action ----
    if input_verdict.should_downgrade:
        # Route to emotion agent in empathic mode
        llm_response = "听起来你很关心这个呢，Niki能陪你聊聊~"
    elif input_verdict.is_escalation:
        # Self-harm: produce a caring response AND flag for parent alert
        llm_response = (
            "宝贝，Niki听到你说的话了。不管发生什么，Niki都在这里陪着你。我们一起想想办法，好不好？"
        )
    else:
        # Normal: use provided mock
        llm_response = mock_response

    # ---- 3. Output gateway ----
    output_gateway = _gateway_for_age(child_age)
    output_verdict = output_gateway.check(llm_response, is_tutoring_response=is_tutoring)

    if output_verdict.action == OutputAction.REGENERATE:
        # Tutoring gave a direct answer — regenerate with guided approach.
        # We preserve `direct_answer_detected=True` so the caller knows a
        # regeneration occurred, even though the fallback text is clean.
        regenerated_text = "我们一起来想一想，先看看题目告诉了我们什么~"
        fallback_verdict = output_gateway.check(
            regenerated_text,
            is_tutoring_response=is_tutoring,
        )
        output_verdict = OutputVerdict(
            action=OutputAction.REGENERATE,
            sanitized_text=fallback_verdict.sanitized_text or regenerated_text,
            direct_answer_detected=True,
            reason="Tutoring agent gave direct answer — regenerated with guided approach",
        )

    return {
        "final_text": output_verdict.sanitized_text,
        "input_action": input_verdict.action.value,
        "input_category": input_verdict.matched_category,
        "output_action": output_verdict.action.value,
        "output_reason": output_verdict.reason,
        "rewrites_applied": [
            {"from": original, "to": replacement}
            for original, replacement in output_verdict.rewrites_applied
        ],
        "direct_answer_detected": output_verdict.direct_answer_detected,
        "sentences_over_limit": output_verdict.sentences_over_limit,
        "parent_alert_required": input_verdict.is_escalation,
    }
