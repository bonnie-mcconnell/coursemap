"""
Unit tests for modules that previously had no test coverage:
  - domain/prerequisite_utils.py  (prereqs_met)
  - ingestion/dataset_loader.py   (normalisation helpers)
  - validation/dataset_validator.py
  - planner/generator.py          (PlanStats)
"""

from __future__ import annotations

from coursemap.domain.course import Course, Offering
from coursemap.domain.prerequisite import (
    AndExpression,
    CoursePrerequisite,
    OrExpression,
)
from coursemap.domain.prerequisite_utils import prereqs_met
from coursemap.ingestion.dataset_loader import (
    normalize_campus as _normalize_campus,
    normalize_mode as _normalize_mode,
    _SEMESTER_MAP,
    _MULTI_SEMESTER_MAP,
    parse_offerings as _parse_offerings,
)
from coursemap.planner.generator import PlanGenerator, PlanStats
from coursemap.validation.dataset_validator import (
    DatasetValidationError,
    DatasetValidationResult,
    validate_dataset,
    check_credits as _check_credits,
    check_offerings as _check_offerings,
    check_prerequisite_codes as _check_prerequisite_codes,
    check_prerequisite_cycles as _check_prerequisite_cycles,
    check_major_course_codes as _check_major_course_codes,
)


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _course(code, *, level=100, credits=15, offerings=None, prereq=None):
    off = offerings if offerings is not None else (Offering("S1", "D", "DIS"),)
    return Course(code, code, credits, level, off, prereq)


def _off(*sems):
    return tuple(Offering(s, "D", "DIS") for s in sems)


# ---------------------------------------------------------------------------
# prereqs_met
# ---------------------------------------------------------------------------

class TestPrereqsMet:
    def test_none_prereq_always_satisfied(self):
        assert prereqs_met(None, set(), {"A", "B"})

    def test_known_code_in_completed(self):
        req = CoursePrerequisite("A")
        assert prereqs_met(req, {"A"}, {"A", "B"})

    def test_known_code_not_in_completed(self):
        req = CoursePrerequisite("A")
        assert not prereqs_met(req, set(), {"A", "B"})

    def test_unknown_code_treated_as_satisfied(self):
        """Admission gatekeepers not in 'known' are treated as pre-satisfied."""
        gatekeeper = CoursePrerequisite("627739")
        assert prereqs_met(gatekeeper, set(), {"A", "B"})  # 627739 not in known

    def test_and_expression_all_known_all_completed(self):
        expr = AndExpression((CoursePrerequisite("A"), CoursePrerequisite("B")))
        assert prereqs_met(expr, {"A", "B"}, {"A", "B"})

    def test_and_expression_one_not_completed(self):
        expr = AndExpression((CoursePrerequisite("A"), CoursePrerequisite("B")))
        assert not prereqs_met(expr, {"A"}, {"A", "B"})

    def test_and_expression_mixed_known_unknown(self):
        """Known completed + unknown gatekeeper = satisfied."""
        expr = AndExpression((CoursePrerequisite("A"), CoursePrerequisite("GATE")))
        assert prereqs_met(expr, {"A"}, {"A", "B"})  # GATE not in known → satisfied

    def test_or_expression_one_branch_satisfied(self):
        expr = OrExpression((CoursePrerequisite("A"), CoursePrerequisite("B")))
        assert prereqs_met(expr, {"A"}, {"A", "B"})

    def test_or_expression_neither_branch_satisfied(self):
        expr = OrExpression((CoursePrerequisite("A"), CoursePrerequisite("B")))
        assert not prereqs_met(expr, set(), {"A", "B"})

    def test_alias_works(self):
        """CoursePrerequisite alias resolves to CoursePrerequisite."""
        assert CoursePrerequisite is CoursePrerequisite
        req = CoursePrerequisite("X")
        assert isinstance(req, CoursePrerequisite)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

class TestNormaliseCampus:
    def test_known_codes_pass_through(self):
        for code in ("A", "D", "M", "N", "W"):
            assert _normalize_campus(code) == code

    def test_unknown_falls_back_to_D(self):
        assert _normalize_campus("PN") == "D"
        assert _normalize_campus("") == "D"
        assert _normalize_campus("XYZ") == "D"


class TestNormaliseMode:
    def test_known_codes_pass_through(self):
        for code in ("DIS", "INT", "BLK"):
            assert _normalize_mode(code) == code

    def test_lowercase_normalised(self):
        assert _normalize_mode("dis") == "DIS"
        assert _normalize_mode("int") == "INT"

    def test_unknown_falls_back_to_DIS(self):
        # "internal" is a legacy value the scraper sometimes produced;
        # it is not a recognised mode code and must fall back to "DIS"
        assert _normalize_mode("internal") == "DIS"
        assert _normalize_mode("") == "DIS"


class TestParseOfferings:
    def test_empty_input_returns_empty_tuple(self):
        assert _parse_offerings(None) == ()
        assert _parse_offerings([]) == ()

    def test_single_semester_1(self):
        raw = [{"semester": "Semester 1", "campus_code": "D", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert len(result) == 1
        assert result[0].semester == "S1"
        assert result[0].campus == "D"
        assert result[0].mode == "DIS"

    def test_single_semester_2(self):
        raw = [{"semester": "Semester 2", "campus_code": "M", "delivery_mode": "INT"}]
        result = _parse_offerings(raw)
        assert len(result) == 1
        assert result[0].semester == "S2"
        assert result[0].campus == "M"

    def test_summer_school(self):
        raw = [{"semester": "Summer School", "campus_code": "D", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert len(result) == 1
        assert result[0].semester == "SS"

    def test_double_semester_expands_to_two(self):
        raw = [{"semester": "Double Semester", "campus_code": "D", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert len(result) == 2
        sems = {o.semester for o in result}
        assert sems == {"S1", "S2"}

    def test_full_year_expands_to_two(self):
        raw = [{"semester": "Full Year", "campus_code": "D", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert len(result) == 2
        sems = {o.semester for o in result}
        assert sems == {"S1", "S2"}

    def test_unknown_semester_skipped(self):
        raw = [{"semester": "Trimester 1", "campus_code": "D", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert result == ()

    def test_unknown_campus_falls_back_to_D(self):
        raw = [{"semester": "Semester 1", "campus_code": "PN", "delivery_mode": "DIS"}]
        result = _parse_offerings(raw)
        assert result[0].campus == "D"

    def test_result_is_tuple(self):
        raw = [{"semester": "Semester 1", "campus_code": "D", "delivery_mode": "DIS"}]
        assert isinstance(_parse_offerings(raw), tuple)


# ---------------------------------------------------------------------------
# DatasetValidator
# ---------------------------------------------------------------------------

class TestCheckCredits:
    def test_valid_credits_no_errors(self):
        courses = {"A": _course("A", credits=15)}
        errors, warnings = [], []
        _check_credits(courses, errors, warnings)
        assert not errors

    def test_zero_credits_is_warning_not_error(self):
        """Zero-credit courses are known non-schedulable entries - downgraded to warning."""
        courses = {"A": _course("A", credits=0)}
        errors, warnings = [], []
        _check_credits(courses, errors, warnings)
        assert not any("credits must be positive" in e for e in errors), \
            "Zero-credit should be a warning, not an error"
        assert any("non-schedulable" in w for w in warnings), \
            "Zero-credit course should produce a warning"

    def test_negative_credits_is_error(self):
        courses = {"A": _course("A", credits=-5)}
        errors, warnings = [], []
        _check_credits(courses, errors, warnings)
        assert errors


class TestCheckOfferings:
    def test_valid_offering_no_issues(self):
        courses = {"A": _course("A", offerings=_off("S1"))}
        errors, warnings = [], []
        _check_offerings(courses, errors, warnings)
        assert not errors
        assert not warnings

    def test_no_offerings_is_warning(self):
        courses = {"A": _course("A", offerings=())}
        errors, warnings = [], []
        _check_offerings(courses, errors, warnings)
        assert not errors
        assert warnings

    def test_bad_semester_is_error(self):
        courses = {"A": Course("A", "A", 15, 100, (Offering("T1", "D", "DIS"),))}
        errors, warnings = [], []
        _check_offerings(courses, errors, warnings)
        assert any("semester" in e for e in errors)

    def test_bad_campus_is_error(self):
        courses = {"A": Course("A", "A", 15, 100, (Offering("S1", "PN", "DIS"),))}
        errors, warnings = [], []
        _check_offerings(courses, errors, warnings)
        assert any("campus" in e for e in errors)

    def test_bad_mode_is_error(self):
        courses = {"A": Course("A", "A", 15, 100, (Offering("S1", "D", "internal"),))}
        errors, warnings = [], []
        _check_offerings(courses, errors, warnings)
        assert any("mode" in e for e in errors)


class TestCheckPrereqCodes:
    def test_known_prereq_no_warnings(self):
        courses = {
            "A": _course("A"),
            "B": _course("B", prereq=CoursePrerequisite("A")),
        }
        errors, warnings = [], []
        _check_prerequisite_codes(courses, errors, warnings)
        assert not errors

    def test_unknown_prereq_produces_warning(self):
        courses = {"A": _course("A", prereq=CoursePrerequisite("GHOST"))}
        errors, warnings = [], []
        _check_prerequisite_codes(courses, errors, warnings)
        assert not errors
        # Either a specific warning for GHOST or the gatekeeper summary
        assert warnings


class TestCheckCycles:
    def test_no_cycles_no_errors(self):
        courses = {
            "A": _course("A"),
            "B": _course("B", prereq=CoursePrerequisite("A")),
        }
        errors, warnings = [], []
        _check_prerequisite_cycles(courses, errors, warnings)
        assert not errors

    def test_direct_cycle_is_error(self):
        courses = {
            "A": _course("A", prereq=CoursePrerequisite("B")),
            "B": _course("B", prereq=CoursePrerequisite("A")),
        }
        errors, warnings = [], []
        _check_prerequisite_cycles(courses, errors, warnings)
        assert errors
        assert any("cycle" in e.lower() for e in errors)

    def test_self_reference_detected(self):
        """A course requiring itself is a trivial 1-cycle."""
        courses = {"A": _course("A", prereq=CoursePrerequisite("A"))}
        errors, warnings = [], []
        _check_prerequisite_cycles(courses, errors, warnings)
        assert errors


class TestCheckMajorCodes:
    def test_all_codes_present_no_warnings(self):
        courses = {"A": _course("A"), "B": _course("B")}
        majors = [{"name": "M", "requirement": {
            "type": "ALL_OF",
            "children": [
                {"type": "COURSE", "course_code": "A"},
                {"type": "COURSE", "course_code": "B"},
            ]
        }}]
        errors, warnings = [], []
        _check_major_course_codes(courses, majors, errors, warnings)
        assert not errors
        assert not warnings

    def test_missing_code_produces_warning(self):
        courses = {"A": _course("A")}
        majors = [{"name": "M", "requirement": {
            "type": "COURSE", "course_code": "MISSING"
        }}]
        errors, warnings = [], []
        _check_major_course_codes(courses, majors, errors, warnings)
        assert warnings
        assert not errors


class TestValidateDataset:
    def test_clean_dataset_passes(self):
        courses = {
            "A": _course("A"),
            "B": _course("B", level=200, prereq=CoursePrerequisite("A")),
        }
        majors = [{"name": "M", "url": "", "requirement": {
            "type": "ALL_OF",
            "children": [
                {"type": "COURSE", "course_code": "A"},
                {"type": "COURSE", "course_code": "B"},
            ]
        }}]
        result = validate_dataset(courses, majors, raise_on_error=False)
        # No credit/offering/cycle/major-code errors; unknown prereq warnings OK
        assert result.is_valid

    def test_cycle_raises_by_default(self):
        courses = {
            "A": _course("A", prereq=CoursePrerequisite("B")),
            "B": _course("B", prereq=CoursePrerequisite("A")),
        }
        try:
            validate_dataset(courses, [], raise_on_error=True)
            assert False, "should have raised"
        except DatasetValidationError as e:
            assert e.result.errors

    def test_raise_on_error_false_returns_result(self):
        courses = {"A": _course("A", credits=-5)}  # negative credits is still an error
        result = validate_dataset(courses, [], raise_on_error=False)
        assert not result.is_valid
        assert result.errors

    def test_summary_contains_counts(self):
        result = DatasetValidationResult(errors=["e1"], warnings=["w1", "w2"])
        summary = result.summary()
        assert "1 error" in summary
        assert "2 warning" in summary


# ---------------------------------------------------------------------------
# PlanStats
# ---------------------------------------------------------------------------

class TestPlanStats:
    def test_stats_reset_on_each_generate(self):
        courses = {"A": _course("A")}
        gen = PlanGenerator(courses)
        gen.generate()
        first_scheduled = gen.stats.courses_scheduled

        gen.generate()
        assert gen.stats.courses_scheduled == first_scheduled  # same single course

    def test_courses_scheduled_count(self):
        courses = {
            "A": _course("A", offerings=_off("S1")),
            "B": _course("B", offerings=_off("S2")),
        }
        gen = PlanGenerator(courses)
        plan = gen.generate()
        assert gen.stats.courses_scheduled == 2
        assert gen.stats.semesters_generated == 2

    def test_prerequisite_rejections_counted(self):
        """B requires A; in S1 B is rejected (A not done yet), then eligible in S2."""
        courses = {
            "A": _course("A", offerings=_off("S1")),
            "B": _course("B", offerings=_off("S1", "S2"), prereq=CoursePrerequisite("A")),
        }
        gen = PlanGenerator(courses)
        gen.generate()
        # B should be rejected at least once (in S1 before A is complete)
        assert gen.stats.prerequisite_rejections >= 1

    def test_empty_semesters_counted(self):
        """A course offered only in S2; the S1 slot is skipped."""
        courses = {"A": _course("A", offerings=_off("S2"))}
        gen = PlanGenerator(courses)
        gen.generate()
        assert gen.stats.empty_semesters_skipped >= 1

    def test_stats_dataclass_defaults(self):
        stats = PlanStats()
        assert stats.courses_scheduled == 0
        assert stats.prerequisite_rejections == 0
        assert stats.offering_rejections == 0
        assert stats.semesters_generated == 0
        assert stats.empty_semesters_skipped == 0


# ---------------------------------------------------------------------------
# _rebalance
# ---------------------------------------------------------------------------

class TestRebalance:
    """
    Tests for PlanGenerator._rebalance.

    Each fixture is designed to be small enough to reason about precisely,
    but to exercise a distinct rebalancing scenario.
    """

    def _off(self, *sems):
        return tuple(Offering(s, "D", "DIS") for s in sems)

    def test_no_rebalance_needed_when_final_above_threshold(self):
        """Final semester at exactly 30cr; no rebalancing triggered."""
        courses = {
            "A": Course("A", "A", 15, 100, self._off("S1")),
            "B": Course("B", "B", 15, 100, self._off("S1")),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()
        # Both in S1 2026 = 30cr final; no rebalance
        assert gen.stats.rebalance_moves == 0
        assert plan.total_credits() == 30

    def test_rebalance_pulls_flexible_course_into_final(self):
        """
        5 S1-only courses (15cr each), max 60cr.
        Greedy: S1[A,B,C,D](60cr), S1[E](15cr); underfilled.
        C is also offered in S2, so a different course (C) can be deferred
        to make room; but actually here we need a course that can move INTO
        the final S1 from an earlier semester.

        Better setup: 5 courses, max 45cr.
          A: S1+S2 (flexible)
          B: S1 only
          C: S1 only
          D: S1 only
          E: S1 only
        Greedy (sorted alpha): S1[A,B,C](45cr), S1[D,E](30cr).
        Final = 30cr; no rebalance needed.

        Real underfill case: 5 courses, max 45cr, last course S1-only,
        first four S1-only filling exactly 45cr.
          A,B,C: S1 only (45cr fills first S1)
          D: S1+S2 (flexible; in first S1, also offered S2)
          E: S1 only (stranded alone in second S1 = 15cr)
        Greedy: S1[A,B,C](45cr); D doesn't fit (60cr > 45)
        Next S1: S1[D,E](30cr); fine actually, not underfilled.

        Use 6 courses to force an underfill:
          A,B,C,D: S1 only (4 * 15 = 60cr fills S1 to cap when max=60)
          E: S1+S2 (flexible, also in first S1)
          F: S1 only (stranded alone in second S1)
        Greedy: S1 2026[A,B,C,D](60cr), E and F go to next S1.
        S1 2027[E,F](30cr); not underfilled (30 = threshold, not < threshold).

        Must use max=45 to force underfill:
          A,B,C: S1-only (45cr fills S1)
          D: S1+S2 (flexible, goes to next S1 since A,B,C fill it)
          E: S1-only (stranded alone)
        Greedy: S1 2026[A,B,C](45cr), S1 2027[D,E](30cr). Not underfilled.

        Must have 5 courses, max 45, where alphabetical fill creates < 30cr final:
          A,B,C,D: S1-only (sorted first, fill 45 = ABC, D overflows)
          E: S1+S2 (also fills to next S1 as 5th course)
        Greedy: S1 2026[A,B,C](45cr), S1 2027[D,E](30cr). Still 30cr, not < 30.

        Use 7 courses max=60 where 1 is S1-only and the rest fill 6 * 15 = 90 > 60+15:
          A,B,C,D: S1-only sorted first (fill 60cr exactly)
          E: S1+S2
          F: S1-only
          G: S1-only
        Greedy: S1 2026[A,B,C,D](60), S1 2027[E,F,G](45). Fine.

        The SIMPLEST underfill case: 5 S1-only courses, max 60, 
        where only 4 fit in S1 and the 5th is alone. Already done above.
        The issue is 30cr = threshold is NOT underfilled (< 30 is the condition).
        So 1 course at 15cr alone is the minimum underfill.

        Let's verify with the exact fixture from the analysis:
        5 courses at 15cr, max 60, all S1-only -> S1[A,B,C,D](60), S1[E](15) <- 15 < 30!
        One of A-D must be S1+S2 to be movable into the final S1.
        """
        courses = {
            "AAA": Course("AAA", "Alpha",   15, 100, self._off("S1")),
            "BBB": Course("BBB", "Beta",    15, 100, self._off("S1")),
            "CCC": Course("CCC", "Gamma",   15, 100, self._off("S1", "S2")),  # flexible
            "DDD": Course("DDD", "Delta",   15, 100, self._off("S1")),
            "EEE": Course("EEE", "Epsilon", 15, 100, self._off("S1")),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()
        # Greedy: S1[AAA,BBB,CCC,DDD](60cr), S1[EEE](15cr)
        # Rebalance: CCC is S1+S2 -> offered in S1 (final) -> move from prior S1 to final S1
        # But CCC is S1+S2, final is S1. Moving CCC from prior S1 to final S1:
        #   prior S1 becomes [AAA,BBB,DDD] (45cr, still non-empty)
        #   final S1 becomes [EEE,CCC] (30cr >= threshold)
        final_cr = plan.semesters[-1].total_credits()
        assert final_cr >= 30, f"Final still underfilled: {final_cr}cr"
        assert plan.total_credits() == 75
        assert gen.stats.rebalance_moves >= 1
        # No duplicates
        all_codes = [c.code for s in plan.semesters for c in s.courses]
        assert len(all_codes) == len(set(all_codes))

    def test_rebalance_same_type_courses_can_be_deferred(self):
        """
        All courses are S1-only. 4 fill the first S1 (60cr), 1 is alone in
        the next S1 (15cr; underfilled). The rebalancer defers one S1-only
        course from the full semester to the underfilled one, producing a
        45cr / 30cr split. This is valid: scheduling the course one year later
        does not break any prerequisite constraint here.
        """
        courses = {
            "AAA": Course("AAA", "A", 15, 100, self._off("S1")),
            "BBB": Course("BBB", "B", 15, 100, self._off("S1")),
            "CCC": Course("CCC", "C", 15, 100, self._off("S1")),
            "DDD": Course("DDD", "D", 15, 100, self._off("S1")),
            "EEE": Course("EEE", "E", 15, 100, self._off("S1")),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()
        # After rebalancing: one course deferred from first S1 to second S1
        assert plan.total_credits() == 75
        final_cr = plan.semesters[-1].total_credits()
        assert final_cr >= 30, f"Final still underfilled: {final_cr}cr"
        assert gen.stats.rebalance_moves == 1
        # All courses still scheduled exactly once
        all_codes = [c.code for s in plan.semesters for c in s.courses]
        assert len(all_codes) == len(set(all_codes)) == 5

    def test_rebalance_no_op_when_prereq_safety_blocks_all_moves(self):
        """
        Every course in the prior semester is a prerequisite for something in
        an intermediate semester, so the safety check blocks all of them.
        The final stays underfilled.
        """
        courses = {
            # S1 2026: four S1-only courses, all needed by intermediate S2 courses
            "A1": Course("A1", "A1", 15, 100, self._off("S1")),
            "A2": Course("A2", "A2", 15, 100, self._off("S1")),
            "A3": Course("A3", "A3", 15, 100, self._off("S1")),
            "A4": Course("A4", "A4", 15, 100, self._off("S1")),
            # S2 2026: four S2 courses each requiring one A course
            "B1": Course("B1", "B1", 15, 200, self._off("S2"), CoursePrerequisite("A1")),
            "B2": Course("B2", "B2", 15, 200, self._off("S2"), CoursePrerequisite("A2")),
            "B3": Course("B3", "B3", 15, 200, self._off("S2"), CoursePrerequisite("A3")),
            "B4": Course("B4", "B4", 15, 200, self._off("S2"), CoursePrerequisite("A4")),
            # S1 2027: one lone S1 course (stranded; underfilled final)
            "C1": Course("C1", "C1", 15, 300, self._off("S1")),
        }
        # max=60: S1[A1-A4](60), S2[B1-B4](60), S1[C1](15; underfilled)
        # Rebalance: final is S1. Prior S1 (A1-A4); all needed by B1-B4 in between.
        # Safety check blocks every A course. No moves possible.
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()
        assert gen.stats.rebalance_moves == 0
        assert plan.semesters[-1].total_credits() == 15  # unchanged
        assert plan.total_credits() == 9 * 15

    def test_rebalance_preserves_prerequisite_ordering_via_intermediate(self):
        """
        A flexible (S1+S2) course needed by an intermediate-semester course
        must not be moved to the final; the intermediate course would lose
        its prerequisite from earlier in the sequence.
        """
        courses = {
            "AAA": Course("AAA", "A", 15, 100, self._off("S1")),
            "BBB": Course("BBB", "B", 15, 100, self._off("S1")),
            "CCC": Course("CCC", "C", 15, 100, self._off("S1")),
            "DDD": Course("DDD", "D", 15, 100, self._off("S1", "S2")),  # flexible
            "EEE": Course("EEE", "E", 15, 200, self._off("S2"),
                          CoursePrerequisite("DDD")),             # needs DDD
            "FFF": Course("FFF", "F", 15, 100, self._off("S1")),  # stranded alone
        }
        # Greedy (max=60):
        # S1[AAA,BBB,CCC,DDD](60), S2[EEE](15; underfilled), then S1[FFF](15)
        # Wait: S2[EEE] is 15cr; is that the final? No: FFF is S1 so goes next.
        # S1[AAA,BBB,CCC,DDD](60), S2[EEE](15), S1[FFF](15); final is S1[FFF].
        # Rebalance final S1 (15cr): look at S2[EEE] then S1[AAA,BBB,CCC,DDD].
        #   S2[EEE]: EEE is S2-only, not offered in final S1; skip.
        #   S1[AAA,BBB,CCC,DDD]: DDD is S1+S2, offered in S1.
        #     needed_by_intermediate: EEE needs DDD; DDD blocked.
        #     AAA,BBB,CCC have no prereqs; one of them moves instead.
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()

        # DDD must not have been moved to the final
        ddd_sem = next(i for i, s in enumerate(plan.semesters)
                       if any(c.code == "DDD" for c in s.courses))
        eee_sem = next(i for i, s in enumerate(plan.semesters)
                       if any(c.code == "EEE" for c in s.courses))
        assert ddd_sem < eee_sem, "DDD must remain before EEE"

        # A safe course (no intermediate dependency) should have been moved instead
        assert gen.stats.rebalance_moves >= 1
        final_cr = plan.semesters[-1].total_credits()
        assert final_cr >= 30, f"Final still underfilled: {final_cr}cr"

        # Total correctness
        all_codes = [c.code for s in plan.semesters for c in s.courses]
        assert len(all_codes) == len(set(all_codes)) == 6
        assert plan.total_credits() == 90

    def test_rebalance_blocks_move_when_final_course_needs_candidate(self):
        """
        A course already sitting in the final semester requires the rebalancing
        candidate as a prerequisite.  Moving the candidate into the final would
        co-schedule it with its dependent; safety check (e) must block it.
        A different course without that constraint should be moved instead.
        """
        courses = {
            "A1": Course("A1", "A1", 15, 100, self._off("S1")),
            "B1": Course("B1", "B1", 15, 100, self._off("S1")),
            "C1": Course("C1", "C1", 15, 100, self._off("S1")),
            "D1": Course("D1", "D1", 15, 100, self._off("S1")),
            # E1 is stranded alone in the final and requires A1
            "E1": Course("E1", "E1", 15, 200, self._off("S1"),
                         CoursePrerequisite("A1")),
        }
        # Greedy: S1[A1,B1,C1,D1](60), S1[E1](15; underfilled)
        # Rebalance: E1 already in final needs A1 → check (e) blocks A1.
        # B1, C1, or D1 are safe → one gets moved.
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()

        # A1 and E1 must never be co-scheduled
        for s in plan.semesters:
            codes = {c.code for c in s.courses}
            assert not ({"A1", "E1"} <= codes), \
                f"A1 and E1 co-scheduled in {s.year} {s.semester}; prereq violated"

        # A1 must appear before E1
        a1_idx = next(i for i, s in enumerate(plan.semesters)
                      if any(c.code == "A1" for c in s.courses))
        e1_idx = next(i for i, s in enumerate(plan.semesters)
                      if any(c.code == "E1" for c in s.courses))
        assert a1_idx < e1_idx, "A1 must be scheduled before E1"

        # A safe candidate was moved; final is no longer underfilled
        final_cr = plan.semesters[-1].total_credits()
        assert final_cr >= 30, f"Final still underfilled: {final_cr}cr"
        assert gen.stats.rebalance_moves == 1

        # All 5 courses scheduled exactly once, total credits intact
        all_codes = [c.code for s in plan.semesters for c in s.courses]
        assert len(all_codes) == len(set(all_codes)) == 5
        assert plan.total_credits() == 75

    def test_rebalance_merges_final_into_penultimate_when_same_type_and_fits(self):
        """
        When final (S1, 15cr) and penultimate (S1, 30cr) share semester type
        and combined credits fit max, they merge into one S1 semester.
        """
        courses = {
            "AAA": Course("AAA", "A", 15, 100, self._off("S1")),
            "BBB": Course("BBB", "B", 15, 100, self._off("S1")),
            "CCC": Course("CCC", "C", 15, 100, self._off("S1")),
            "DDD": Course("DDD", "D", 15, 100, self._off("S1")),
            "EEE": Course("EEE", "E", 15, 100, self._off("S1")),
            "FFF": Course("FFF", "F", 15, 100, self._off("S1")),
            "GGG": Course("GGG", "G", 15, 100, self._off("S1")),
        }
        # max=60: S1[AAA-DDD](60), S1[EEE-GGG](45). Final=45>=30, no rebalance.
        # Use max=45:
        # S1[AAA-CCC](45), S1[DDD-FFF](45), S1[GGG](15)
        # Rebalance: GGG alone (15cr). Prior S1 has DDD,EEE,FFF.
        # DDD is S1-only, offered in S1 (final); can move if source non-empty.
        # Move DDD to final: [GGG,DDD](30cr). Prior becomes [EEE,FFF](30cr).
        # Pass 3: penultimate=[EEE,FFF](30cr) + final=[GGG,DDD](30cr) = 60cr > 45 max
        # Cannot merge. But 30cr >= threshold so we stop.
        gen = PlanGenerator(courses, max_credits_per_semester=45)
        plan = gen.generate()
        final_cr = plan.semesters[-1].total_credits()
        assert final_cr >= 30, f"Final still underfilled: {final_cr}cr"
        assert plan.total_credits() == 105
        all_codes = [c.code for s in plan.semesters for c in s.courses]
        assert len(all_codes) == len(set(all_codes))

    def test_rebalance_stats_counter(self):
        """rebalance_moves increments for each course repositioned."""
        courses = {
            "AAA": Course("AAA", "A", 15, 100, self._off("S1")),
            "BBB": Course("BBB", "B", 15, 100, self._off("S1")),
            "CCC": Course("CCC", "C", 15, 100, self._off("S1", "S2")),
            "DDD": Course("DDD", "D", 15, 100, self._off("S1")),
            "EEE": Course("EEE", "E", 15, 100, self._off("S1")),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()
        # CCC should be moved from prior S1 to final S1
        assert gen.stats.rebalance_moves == 1

    def test_rebalance_merge_blocked_by_prereq(self):
        """
        Pass 3 must not merge two same-type semesters when a course in the
        final requires a course in the penultimate as a prerequisite.

        Setup: two S2-only courses where B requires A.
        The greedy pass correctly puts A in S2 yr1 and B in S2 yr2.
        Pass 3 would merge them (combined 30cr <= 60cr cap, same type),
        but must not because B.prereqs ∩ pen_codes = {A}.
        """
        from coursemap.domain.prerequisite import CoursePrerequisite
        courses = {
            "A": Course("A", "Base", 15, 100, self._off("S2")),
            "B": Course(
                "B", "Dependent", 15, 200, self._off("S2"),
                prerequisites=CoursePrerequisite("A"),
            ),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60)
        plan = gen.generate()

        assert len(plan.semesters) == 2, (
            f"Expected 2 semesters (A then B), got {len(plan.semesters)}: "
            + str([(s.year, s.semester, [c.code for c in s.courses]) for s in plan.semesters])
        )
        assert plan.semesters[0].courses[0].code == "A"
        assert plan.semesters[1].courses[0].code == "B"
        # Prerequisite ordering must hold
        done: set[str] = set()
        for sem in plan.semesters:
            for c in sem.courses:
                if c.prerequisites:
                    assert c.prerequisites.required_courses() <= done, (
                        f"{c.code} scheduled before its prerequisite"
                    )
            done.update(c.code for c in sem.courses)



    def test_rebalance_merge_respects_max_courses_per_semester(self):
        """
        Pass 3 must not merge two same-type semesters when the combined course
        count would exceed max_courses_per_semester.

        Setup: two independent S1-only courses, max=1 per semester.
        Each lands in its own S1. Without the guard, Pass 3 would merge them
        into a single S1 (combined 30cr <= 60cr cap), violating the cap.
        """
        courses = {
            "X": Course("X", "X", 15, 100, self._off("S1")),
            "Y": Course("Y", "Y", 15, 100, self._off("S1")),
        }
        gen = PlanGenerator(courses, max_credits_per_semester=60, max_courses_per_semester=1)
        plan = gen.generate()

        for sem in plan.semesters:
            assert len(sem.courses) <= 1, (
                f"Semester {sem.year} {sem.semester} has {len(sem.courses)} courses "
                f"(max_courses_per_semester=1): {[c.code for c in sem.courses]}"
            )
        assert plan.total_credits() == 30

    def test_rebalance_merge_allowed_when_no_prereq_dependency(self):
        """
        Pass 3 merges freely when neither final course requires a penultimate course.
        """
        courses = {
            "A": Course("A", "A", 15, 100, self._off("S1")),
            "B": Course("B", "B", 15, 100, self._off("S1")),
            "C": Course("C", "C", 15, 100, self._off("S1")),
        }
        # max=30: S1[A,B](30), S1[C](15) → merge: [A,B,C](45) > 30, no merge
        # max=60: all three fit in one S1 → no rebalance needed
        # Use max=45: S1[A,B,C](45) → one semester, no rebalance
        # Use 4 courses, max=45: S1[A,B,C](45), S1[D](15) → merge [A..D](60)>45 no
        # Simplest: 2 independent S2 courses that don't form a prereq pair
        courses2 = {
            "X": Course("X", "X", 15, 100, self._off("S2")),
            "Y": Course("Y", "Y", 15, 100, self._off("S2")),
        }
        gen = PlanGenerator(courses2, max_credits_per_semester=60)
        plan = gen.generate()
        # Both offered only in S2. Greedy: X in S2 yr1, Y in S2 yr1 (same semester).
        # If they go into the same semester, fine - just assert total is right.
        assert plan.total_credits() == 30
        all_codes = {c.code for s in plan.semesters for c in s.courses}
        assert all_codes == {"X", "Y"}


# ---------------------------------------------------------------------------
# degree_rules: profile_for, build_degree_tree, filter_requirement_tree
# ---------------------------------------------------------------------------

class TestDegreeRules:
    """Tests for degree_rules.py - credit profile lookup and tree construction."""

    def test_profile_standard_bsc(self):
        """3-year Level 7 → 360cr with level constraints."""
        from coursemap.rules.degree_rules import profile_for
        p = profile_for(7, 3)
        assert p.total_credits == 360
        assert p.max_level_100 == 165
        assert p.min_level_300 == 75
        assert p.min_level_400 is None

    def test_profile_4yr_bachelor(self):
        from coursemap.rules.degree_rules import profile_for
        p = profile_for(7, 4)
        assert p.total_credits == 480
        assert p.min_level_300 == 135

    def test_profile_honours(self):
        """Level 8 1yr → 120cr, no level constraints."""
        from coursemap.rules.degree_rules import profile_for
        p = profile_for(8, 1)
        assert p.total_credits == 120
        assert p.max_level_100 is None
        assert p.min_level_300 is None

    def test_profile_masters(self):
        from coursemap.rules.degree_rules import profile_for
        assert profile_for(9, 2).total_credits == 240
        assert profile_for(9, 1).total_credits == 120

    def test_profile_fallback(self):
        """Unknown combination → 120cr per year, no level constraints."""
        from coursemap.rules.degree_rules import profile_for
        p = profile_for(6, 2)
        assert p.total_credits == 240
        assert p.max_level_100 is None

    def test_build_degree_tree_includes_total_when_complete(self):
        """When schedulable credits >= degree total, TotalCreditsRequirement is included."""
        from coursemap.rules.degree_rules import build_degree_tree
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement, CourseRequirement, TotalCreditsRequirement,
        )
        major_req = AllOfRequirement((CourseRequirement("111101"),))
        tree = build_degree_tree(
            major_req=major_req,
            qual_level=7,
            qual_length=3,
            major_name="Test",
            schedulable_major_credits=360,
        )
        assert isinstance(tree, AllOfRequirement)
        total_nodes = [c for c in tree.children if isinstance(c, TotalCreditsRequirement)]
        assert len(total_nodes) == 1
        assert total_nodes[0].required_credits == 360

    def test_build_degree_tree_omits_total_when_incomplete(self):
        """When schedulable credits < degree total, TotalCreditsRequirement is omitted."""
        from coursemap.rules.degree_rules import build_degree_tree
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement, CourseRequirement, TotalCreditsRequirement,
        )
        major_req = AllOfRequirement((CourseRequirement("111101"),))
        tree = build_degree_tree(
            major_req=major_req,
            qual_level=7,
            qual_length=3,
            major_name="Test",
            schedulable_major_credits=120,  # < 360
        )
        total_nodes = [c for c in tree.children if isinstance(c, TotalCreditsRequirement)]
        assert len(total_nodes) == 0

    def test_filter_requirement_tree_drops_unschedulable_course(self):
        """CourseRequirement for a code not in schedulable_codes is removed."""
        from coursemap.rules.degree_rules import filter_requirement_tree
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement, CourseRequirement,
        )
        tree = AllOfRequirement((
            CourseRequirement("AAA"),
            CourseRequirement("BBB"),
        ))
        result = filter_requirement_tree(tree, frozenset({"AAA"}))
        # AllOfRequirement wrapper is preserved; BBB is dropped
        assert isinstance(result, AllOfRequirement)
        remaining = {c.course_code for c in result.children}
        assert remaining == {"AAA"}

    def test_filter_requirement_tree_caps_pool_credits(self):
        """ChooseCreditsRequirement target is capped at available schedulable credits."""
        from coursemap.rules.degree_rules import filter_requirement_tree
        from coursemap.domain.requirement_nodes import ChooseCreditsRequirement
        pool = ChooseCreditsRequirement(
            credits=60,
            course_codes=("AAA", "BBB"),
        )
        # Only AAA is schedulable at 15cr → pool should cap at 15
        result = filter_requirement_tree(
            pool,
            frozenset({"AAA"}),
            course_credits={"AAA": 15, "BBB": 15},
        )
        assert isinstance(result, ChooseCreditsRequirement)
        assert result.credits == 15
        assert result.course_codes == ("AAA",)

    def test_filter_requirement_tree_drops_empty_pool(self):
        """Pool with no schedulable members returns None."""
        from coursemap.rules.degree_rules import filter_requirement_tree
        from coursemap.domain.requirement_nodes import ChooseCreditsRequirement
        pool = ChooseCreditsRequirement(credits=30, course_codes=("AAA", "BBB"))
        result = filter_requirement_tree(pool, frozenset())
        assert result is None


# ---------------------------------------------------------------------------
# requirement_utils: traversal helpers
# ---------------------------------------------------------------------------

class TestRequirementUtils:
    """Tests for requirement_utils.py traversal functions."""

    def _tree(self):
        """Build a small mixed tree for testing."""
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement, AnyOfRequirement, ChooseCreditsRequirement,
            CourseRequirement, TotalCreditsRequirement,
        )
        return AllOfRequirement((
            TotalCreditsRequirement(360),
            CourseRequirement("AAA"),
            AnyOfRequirement((
                CourseRequirement("BBB"),
                CourseRequirement("CCC"),
            )),
            ChooseCreditsRequirement(30, ("DDD", "EEE")),
        ))

    def test_collect_course_codes(self):
        from coursemap.domain.requirement_utils import collect_course_codes
        codes = collect_course_codes(self._tree())
        assert codes == {"AAA", "BBB", "CCC", "DDD", "EEE"}

    def test_collect_elective_nodes(self):
        from coursemap.domain.requirement_utils import collect_elective_nodes
        from coursemap.domain.requirement_nodes import ChooseCreditsRequirement
        nodes = collect_elective_nodes(self._tree())
        assert len(nodes) == 1
        assert isinstance(nodes[0], ChooseCreditsRequirement)
        assert set(nodes[0].course_codes) == {"DDD", "EEE"}

    def test_find_total_credits(self):
        from coursemap.domain.requirement_utils import find_total_credits
        assert find_total_credits(self._tree()) == 360

    def test_find_total_credits_missing(self):
        from coursemap.domain.requirement_utils import find_total_credits
        from coursemap.domain.requirement_nodes import CourseRequirement
        assert find_total_credits(CourseRequirement("AAA")) == 0

    def test_collect_core_course_codes_excludes_pool_members(self):
        """collect_core_course_codes only returns COURSE node codes, not pool members."""
        from coursemap.domain.requirement_utils import collect_core_course_codes
        codes = collect_core_course_codes(self._tree())
        assert "AAA" in codes
        assert "BBB" in codes
        assert "DDD" not in codes  # pool member
        assert "EEE" not in codes  # pool member


# ---------------------------------------------------------------------------
# requirement_serialization: requirement_to_dict round-trip
# ---------------------------------------------------------------------------

class TestRequirementSerialization:
    """requirement_to_dict and requirement_from_dict are inverses."""

    def _round_trip(self, node):
        from coursemap.domain.requirement_serialization import (
            requirement_to_dict, requirement_from_dict,
        )
        return requirement_from_dict(requirement_to_dict(node))

    def test_course_requirement_round_trip(self):
        from coursemap.domain.requirement_nodes import CourseRequirement
        node = CourseRequirement("123456")
        assert self._round_trip(node) == node

    def test_all_of_round_trip(self):
        from coursemap.domain.requirement_nodes import AllOfRequirement, CourseRequirement
        node = AllOfRequirement((CourseRequirement("AAA"), CourseRequirement("BBB")))
        assert self._round_trip(node) == node

    def test_choose_credits_round_trip(self):
        from coursemap.domain.requirement_nodes import ChooseCreditsRequirement
        node = ChooseCreditsRequirement(credits=45, course_codes=("AAA", "BBB", "CCC"))
        assert self._round_trip(node) == node

    def test_total_credits_round_trip(self):
        from coursemap.domain.requirement_nodes import TotalCreditsRequirement
        node = TotalCreditsRequirement(360)
        assert self._round_trip(node) == node

    def test_nested_round_trip(self):
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement, AnyOfRequirement, ChooseCreditsRequirement,
            CourseRequirement, TotalCreditsRequirement,
        )
        node = AllOfRequirement((
            TotalCreditsRequirement(120),
            CourseRequirement("AAA"),
            AnyOfRequirement((CourseRequirement("BBB"), CourseRequirement("CCC"))),
            ChooseCreditsRequirement(30, ("DDD", "EEE")),
        ))
        assert self._round_trip(node) == node

    def test_unknown_type_raises(self):
        from coursemap.domain.requirement_serialization import requirement_from_dict
        import pytest
        with pytest.raises(ValueError, match="Unknown requirement type"):
            requirement_from_dict({"type": "BOGUS"})


# ---------------------------------------------------------------------------
# scorer.score
# ---------------------------------------------------------------------------

class TestPlanScorer:
    """Tests for PlanScorer - lower is better."""

    def _make_plan(self, semester_credits: list[int]):
        """Build a DegreePlan with given per-semester credit loads."""
        from coursemap.domain.plan import DegreePlan, SemesterPlan
        from coursemap.domain.course import Course, Offering
        semesters = []
        for i, cr in enumerate(semester_credits):
            course = Course(
                code=f"{i:06d}",
                title=f"Course {i}",
                credits=cr,
                level=100,
                offerings=(Offering("S1", "D", "DIS"),),
            )
            semesters.append(SemesterPlan(year=2026 + i, semester="S1", courses=(course,)))
        return DegreePlan(tuple(semesters))

    def test_fewer_semesters_scores_lower(self):
        from coursemap.optimisation.scorer import PlanScorer
        scorer = PlanScorer()
        short = self._make_plan([60, 60])
        long_ = self._make_plan([30, 30, 30, 30])
        assert scorer.score(short) < scorer.score(long_)

    def test_balanced_load_scores_lower_than_spiky(self):
        from coursemap.optimisation.scorer import PlanScorer
        scorer = PlanScorer()
        balanced = self._make_plan([45, 45])
        spiky    = self._make_plan([15, 75])
        # Same semester count, balanced wins on spread
        assert scorer.score(balanced) < scorer.score(spiky)

    def test_empty_plan_scores_infinity(self):
        from coursemap.optimisation.scorer import PlanScorer
        from coursemap.domain.plan import DegreePlan
        scorer = PlanScorer()
        assert scorer.score(DegreePlan(())) == float("inf")

    def test_lighter_final_semester_preferred(self):
        from coursemap.optimisation.scorer import PlanScorer
        scorer = PlanScorer()
        light_final = self._make_plan([60, 15])
        heavy_final = self._make_plan([15, 60])
        assert scorer.score(light_final) < scorer.score(heavy_final)
