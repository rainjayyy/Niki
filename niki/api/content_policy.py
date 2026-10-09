"""Content policy API endpoints — PRD #32, #34.

Demonstration endpoints for #13 vertical slice:
  POST /policy/blacklist/{family_id}/add     — add a blacklisted topic
  POST /policy/blacklist/{family_id}/remove  — remove a blacklisted topic
  GET  /policy/blacklist/{family_id}         — list blacklisted topics
  POST /policy/blacklist/{family_id}/evaluate — evaluate text for blacklist matches
  GET  /policy/age/{child_id}               — get child's age band
  POST /policy/age/grade                    — grade a concept for a child age
  POST /policy/downgrade/evaluate           — apply all downgrades to text
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from niki.auth.account import ParentAccount
from niki.auth.dependencies import get_current_parent

from niki.content_policy import (
    AgeGrader,
    BlacklistAction,
    ContentDowngrader,
    FamilyBlacklist,
)

router = APIRouter(
    prefix="/policy",
    tags=[
        {"name": "content-policy", "description": "Content policy API endpoints — PRD #32, #34."}
    ],
)

# Module-level state (per-family)
_blacklists: dict[str, FamilyBlacklist] = {}
_age_graders: dict[str, AgeGrader] = {}
_downgraders: dict[str, ContentDowngrader] = {}


def _get_blacklist(family_id: str) -> FamilyBlacklist:
    if family_id not in _blacklists:
        _blacklists[family_id] = FamilyBlacklist()
    return _blacklists[family_id]


def _get_age_grader(child_id: str, child_age: int) -> AgeGrader:
    if child_id not in _age_graders:
        _age_graders[child_id] = AgeGrader(child_age)
    return _age_graders[child_id]


def _get_downgrader(family_id: str, child_id: str, child_age: int) -> ContentDowngrader:
    key = f"{family_id}:{child_id}"
    if key not in _downgraders:
        blacklist = _get_blacklist(family_id)
        grader = _get_age_grader(child_id, child_age)
        _downgraders[key] = ContentDowngrader(blacklist, grader)
    return _downgraders[key]


# ---- Blacklist endpoints ----


@router.post(
    "/blacklist/{family_id}/add",
    summary="Add a topic to the family blacklist",
    description="Add a topic to the family blacklist.",
)
async def add_blacklist(
    family_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Add a topic to the family blacklist."""
    topic = payload.get("topic", "")
    action_str = payload.get("action", "downgrade")
    added_by = payload.get("added_by", "")

    try:
        action = BlacklistAction(action_str)
    except ValueError:
        action = BlacklistAction.DOWNGRADE

    blacklist = _get_blacklist(family_id)
    entry = blacklist.add(topic, action=action, added_by=added_by)
    return {"entry": entry.to_dict(), "blacklist_size": blacklist.size}


@router.post(
    "/blacklist/{family_id}/remove",
    summary="Remove a topic from the family blacklist",
    description="Remove a topic from the family blacklist.",
)
async def remove_blacklist(
    family_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Remove a topic from the family blacklist."""
    topic = payload.get("topic", "")
    blacklist = _get_blacklist(family_id)
    removed = blacklist.remove(topic)
    return {"removed": removed, "blacklist_size": blacklist.size}


@router.get(
    "/blacklist/{family_id}",
    summary="List all blacklisted topics for the family",
    description="List all blacklisted topics for the family.",
)
async def get_blacklist(
    family_id: str,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """List all blacklisted topics for the family."""
    return _get_blacklist(family_id).to_dict()


@router.post(
    "/blacklist/{family_id}/evaluate",
    summary="Evaluate text for blacklist matches",
    description="Evaluate text for blacklist matches.",
)
async def evaluate_blacklist(
    family_id: str,
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Evaluate text for blacklist matches."""
    text = payload.get("text", "")
    matches = _get_blacklist(family_id).match(text)
    return {
        "match_count": len(matches),
        "matches": [m.to_dict() for m in matches],
    }


# ---- Age grading endpoints ----


@router.get(
    "/age/{child_id}",
    summary="Get the age band for a child",
    description="Get the age band for a child.",
)
async def get_age_band(
    child_id: str,
    child_age: int = 7,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Get the age band for a child."""
    grader = _get_age_grader(child_id, child_age)
    return {
        "child_age": child_age,
        "age_band": int(grader.child_band),
    }


@router.post(
    "/age/grade",
    summary="Grade a concept for a child's age",
    description="Grade a concept for a child's age.",
)
async def grade_concept(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Grade a concept for a child's age."""
    child_age = int(payload.get("child_age", 7))
    concept = payload.get("concept", "")
    grader = AgeGrader(child_age)
    grade = grader.grade(concept)
    return grade.to_dict()


@router.post(
    "/age/grade-text",
    summary="Grade all concepts mentioned in text",
    description="Grade all concepts mentioned in text.",
)
async def grade_text(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Grade all concepts mentioned in text."""
    child_age = int(payload.get("child_age", 7))
    text = payload.get("text", "")
    grader = AgeGrader(child_age)
    grades = grader.grade_text(text)
    return {
        "count": len(grades),
        "grades": [g.to_dict() for g in grades],
    }


# ---- Combined downgrader endpoint ----


@router.post(
    "/downgrade/evaluate",
    summary="Apply all downgrade rules (blacklist + age) to text",
    description="Apply all downgrade rules (blacklist + age) to text.",
)
async def evaluate_downgrade(
    payload: dict,
    parent: ParentAccount = Depends(get_current_parent),
) -> dict:
    """Apply all downgrade rules (blacklist + age) to text."""
    family_id = payload.get("family_id", "")
    child_id = payload.get("child_id", "")
    child_age = int(payload.get("child_age", 7))
    text = payload.get("text", "")

    downgrader = _get_downgrader(family_id, child_id, child_age)
    decision = downgrader.evaluate(text)
    return decision.to_dict()
