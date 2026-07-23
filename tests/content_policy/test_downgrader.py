"""Tests for the content downgrader."""

import pytest

from niki.content_policy.blacklist import (
    BlacklistAction,
    FamilyBlacklist,
)
from niki.content_policy.downgrader import (
    ContentDowngrader,
    DowngradeAction,
    DowngradeDecision,
)
from niki.content_policy.grader import AgeGrader


@pytest.fixture
def downgrader() -> ContentDowngrader:
    blacklist = FamilyBlacklist()
    grader = AgeGrader(child_age=4)  # toddler
    return ContentDowngrader(blacklist, grader)


class TestDowngraderPass:
    def test_pass_on_safe_text(self, downgrader: ContentDowngrader) -> None:
        decision = downgrader.evaluate("我喜欢恐龙")
        assert decision.action == DowngradeAction.PASS
        assert decision.rewritten_text is None

    def test_pass_on_empty(self, downgrader: ContentDowngrader) -> None:
        decision = downgrader.evaluate("")
        assert decision.action == DowngradeAction.PASS


class TestDowngraderBlacklist:
    def test_blacklist_triggers_deflect(self) -> None:
        blacklist = FamilyBlacklist()
        blacklist.add("打游戏", BlacklistAction.DOWNGRADE)
        grader = AgeGrader(child_age=7)
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("我想打游戏")
        assert decision.action == DowngradeAction.DEFLECT
        assert decision.blocked_concept == "打游戏"
        assert "打游戏" not in decision.rewritten_text  # Replaced

    def test_blacklist_redirect_action(self) -> None:
        blacklist = FamilyBlacklist()
        blacklist.add("恐怖", BlacklistAction.REDIRECT)
        grader = AgeGrader(child_age=7)
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("看恐怖电影")
        assert decision.action == DowngradeAction.REDIRECT
        assert decision.blocked_concept == "恐怖"
        assert "恐怖" not in decision.rewritten_text

    def test_blacklist_apologize_action(self) -> None:
        blacklist = FamilyBlacklist()
        blacklist.add("test_topic", BlacklistAction.APOLOGIZE)
        grader = AgeGrader(child_age=7)
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("this is a test_topic sentence")
        assert decision.action == DowngradeAction.DEFLECT
        assert decision.rewritten_text  # Should be the apology message

    def test_blacklist_match_recorded(self) -> None:
        blacklist = FamilyBlacklist()
        blacklist.add("X")
        grader = AgeGrader(child_age=7)
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("this is X content")
        assert len(decision.blacklist_matches) == 1


class TestDowngraderAge:
    def test_too_hard_triggers_simplify(self) -> None:
        blacklist = FamilyBlacklist()
        grader = AgeGrader(child_age=4)  # toddler
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("量子力学 is interesting")
        # Should either SIMPLIFY (if we have a replacement) or SOFTEN
        assert decision.action in (
            DowngradeAction.SIMPLIFY,
            DowngradeAction.SOFTEN,
        )

    def test_simplify_uses_known_replacement(self) -> None:
        from niki.content_policy.downgrader import SIMPLIFICATIONS

        blacklist = FamilyBlacklist()
        grader = AgeGrader(child_age=4)
        d = ContentDowngrader(blacklist, grader)

        # "分数" has a known simplification
        decision = d.evaluate("我想学分数")
        if "分数" in SIMPLIFICATIONS:
            assert decision.action == DowngradeAction.SIMPLIFY
            assert SIMPLIFICATIONS["分数"] in decision.rewritten_text

    def test_age_grade_recorded(self) -> None:
        blacklist = FamilyBlacklist()
        grader = AgeGrader(child_age=4)
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("quantum physics 量子力学 concepts")
        if decision.action in (DowngradeAction.SIMPLIFY, DowngradeAction.SOFTEN):
            assert len(decision.content_grades) > 0


class TestDowngraderPriority:
    def test_blacklist_takes_priority(self) -> None:
        """Blacklist should be checked first, even if content is also too hard."""
        blacklist = FamilyBlacklist()
        blacklist.add("量子力学", BlacklistAction.DOWNGRADE)
        grader = AgeGrader(child_age=4)  # also too hard
        d = ContentDowngrader(blacklist, grader)

        decision = d.evaluate("explain 量子力学")
        # Blacklist match wins → DEFLECT, not SIMPLIFY
        assert decision.action == DowngradeAction.DEFLECT
        assert decision.blocked_concept == "量子力学"


class TestDecisionToDict:
    def test_to_dict_pass(self) -> None:
        decision = DowngradeDecision(
            action=DowngradeAction.PASS,
            original_text="hello",
        )
        d = decision.to_dict()
        assert d["action"] == "pass"
        assert d["original_text"] == "hello"

    def test_to_dict_blocked(self) -> None:
        decision = DowngradeDecision(
            action=DowngradeAction.DEFLECT,
            original_text="bad text",
            rewritten_text="rewritten",
            blocked_concept="bad",
        )
        d = decision.to_dict()
        assert d["action"] == "deflect"
        assert d["blocked_concept"] == "bad"
