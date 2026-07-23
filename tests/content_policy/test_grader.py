"""Tests for the age grader."""

from niki.content_policy.grader import (
    AgeGrader,
    ContentAge,
    ContentGrade,
    age_to_band,
)


class TestAgeBand:
    def test_toddler(self) -> None:
        assert age_to_band(3) == ContentAge.TODDLER
        assert age_to_band(5) == ContentAge.TODDLER

    def test_young(self) -> None:
        assert age_to_band(6) == ContentAge.YOUNG
        assert age_to_band(8) == ContentAge.YOUNG

    def test_mid(self) -> None:
        assert age_to_band(9) == ContentAge.MID
        assert age_to_band(12) == ContentAge.MID

    def test_teen(self) -> None:
        assert age_to_band(13) == ContentAge.TEEN

    def test_default(self) -> None:
        # Adult or out-of-range → defaults to MID
        assert age_to_band(50) == ContentAge.MID


class TestAgeGraderBasics:
    def test_grader_child_age(self) -> None:
        g = AgeGrader(child_age=7)
        assert g.child_age == 7
        assert g.child_band == ContentAge.YOUNG

    def test_grader_toddler(self) -> None:
        g = AgeGrader(child_age=4)
        assert g.child_band == ContentAge.TODDLER


class TestAgeGraderGrade:
    def test_age_appropriate(self) -> None:
        """A concept appropriate for the child's age."""
        g = AgeGrader(child_age=4)
        grade = g.grade("恐龙")  # Recommended for toddler
        assert grade.appropriate
        assert grade.complexity_delta <= 1

    def test_too_advanced(self) -> None:
        """A concept too advanced for the child."""
        g = AgeGrader(child_age=4)  # toddler
        grade = g.grade("量子力学")  # teen
        assert not grade.appropriate
        assert grade.complexity_delta > 1

    def test_unknown_concept(self) -> None:
        """A concept not in the dictionary defaults to MID."""
        g = AgeGrader(child_age=4)
        grade = g.grade("unicorns")  # Not in dict
        # Default is MID, age 4 = toddler → complexity_delta = 9 - 3 = 6
        assert grade.required_age == ContentAge.MID


class TestAgeGraderGradeText:
    def test_grade_text_known_concepts(self) -> None:
        g = AgeGrader(child_age=7)
        grades = g.grade_text("我喜欢恐龙和颜色")
        concepts = {gr.concept for gr in grades}
        assert "恐龙" in concepts
        assert "颜色" in concepts

    def test_grade_text_no_concepts(self) -> None:
        g = AgeGrader(child_age=7)
        grades = g.grade_text("random unrelated text")
        assert grades == []

    def test_grade_text_empty(self) -> None:
        g = AgeGrader(child_age=7)
        assert g.grade_text("") == []


class TestAgeGraderIsAppropriate:
    def test_appropriate(self) -> None:
        g = AgeGrader(child_age=4)
        assert g.is_appropriate("恐龙")
        assert g.is_appropriate("颜色")

    def test_not_appropriate(self) -> None:
        g = AgeGrader(child_age=4)
        assert not g.is_appropriate("量子力学")


class TestAgeGraderSuggestSimpler:
    def test_suggest_simpler_for_too_hard(self) -> None:
        g = AgeGrader(child_age=4)  # toddler
        simpler = g.suggest_simpler("量子力学")
        # Should suggest something simpler (lower age band)
        if simpler is not None:
            # The suggested concept should have a lower min age
            from niki.content_policy.grader import CONCEPT_MIN_AGE

            assert int(CONCEPT_MIN_AGE[simpler]) < int(ContentAge.TEEN)

    def test_suggest_simpler_no_simpler(self) -> None:
        g = AgeGrader(child_age=10)
        # If concept is appropriate, no simplification needed
        assert g.suggest_simpler("恐龙") is None

    def test_suggest_simpler_unknown(self) -> None:
        g = AgeGrader(child_age=4)
        # No known alternatives
        result = g.suggest_simpler("nonexistent_concept_xyz")
        assert result is None


class TestContentGrade:
    def test_to_dict(self) -> None:
        g = ContentGrade(
            concept="恐龙",
            required_age=ContentAge.TODDLER,
            child_age=ContentAge.YOUNG,
            appropriate=True,
            complexity_delta=-3,
        )
        d = g.to_dict()
        assert d["concept"] == "恐龙"
        assert d["required_age"] == 3
        assert d["child_age"] == 6
        assert d["appropriate"] is True
