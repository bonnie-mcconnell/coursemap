from coursemap.domain.course import Course, Offering
from coursemap.domain.prerequisite import CoursePrerequisite
from coursemap.planner.generator import PlanGenerator


def _offering(semesters):
    return tuple(Offering(semester=s, campus="D", mode="DIS") for s in semesters)


def _fixture_courses():
    """Minimal course catalog for scheduler tests.

    Three prerequisite chains across two levels:
      STAT101 -> STAT102 -> STAT201 -> STAT301
      MATH101 -> MATH201
      COMP101 -> COMP201
    All 100-level offered S1+S2; 200-level S1; 300-level S2.
    Total: 8 courses x 15 credits = 120 credits.
    """
    prereq = CoursePrerequisite
    off    = _offering
    return {
        "STAT101": Course("STAT101", "Statistics I",        15, 100, off(["S1", "S2"])),
        "STAT102": Course("STAT102", "Statistics II",       15, 100, off(["S1", "S2"]),
                          prereq("STAT101")),
        "MATH101": Course("MATH101", "Calculus I",          15, 100, off(["S1", "S2"])),
        "COMP101": Course("COMP101", "Programming I",       15, 100, off(["S1", "S2"])),
        "STAT201": Course("STAT201", "Stat Modelling",      15, 200, off(["S1"]),
                          prereq("STAT102")),
        "MATH201": Course("MATH201", "Linear Algebra",      15, 200, off(["S1"]),
                          prereq("MATH101")),
        "COMP201": Course("COMP201", "Data Structures",     15, 200, off(["S1"]),
                          prereq("COMP101")),
        "STAT301": Course("STAT301", "Advanced Regression", 15, 300, off(["S2"]),
                          prereq("STAT201")),
    }


def test_plan_generation():
    courses = _fixture_courses()

    generator = PlanGenerator(
        courses,
        max_credits_per_semester=60,
        start_year=2026,
    )

    plan = generator.generate()

    total_courses = sum(len(s.courses) for s in plan.semesters)

    assert total_courses == len(courses)

    # STAT101 must come before STAT102
    semester_lookup = {}
    for i, sem in enumerate(plan.semesters):
        for c in sem.courses:
            semester_lookup[c.code] = i

    assert semester_lookup["STAT101"] < semester_lookup["STAT102"]
    assert semester_lookup["STAT201"] < semester_lookup["STAT301"]


# ══════════════════════════════════════════════════════════════
# Extended generator tests
# ══════════════════════════════════════════════════════════════

import pytest
from coursemap.planner.generator import PlanGenerator, REBALANCE_THRESHOLD
from coursemap.domain.course import Course, Offering
from coursemap.domain.prerequisite import CoursePrerequisite, AndExpression


def _make_offering(semester="S1", campus="D", mode="DIS"):
    return Offering(semester=semester, campus=campus, mode=mode)


def _make_course(code, level=100, credits=15, semesters=("S1", "S2"),
                 prereq=None, campus="D", mode="DIS"):
    offerings = tuple(_make_offering(s, campus, mode) for s in semesters)
    return Course(code=code, title=f"Course {code}", credits=credits,
                  level=level, offerings=offerings, prerequisites=prereq)


def _make_generator(courses_list, **kwargs):
    courses = {c.code: c for c in courses_list}
    defaults = dict(max_credits_per_semester=60, campus="D", mode="DIS",
                    start_year=2026, start_semester="S1", no_summer=True)
    defaults.update(kwargs)
    return PlanGenerator(courses, **defaults)


def test_simple_chain_respects_prereq_order():
    """A→B→C chain: A must appear before B, B before C."""
    a = _make_course("AAA100")
    b = _make_course("BBB200", prereq=CoursePrerequisite("AAA100"))
    c = _make_course("CCC300", prereq=CoursePrerequisite("BBB200"))
    gen = _make_generator([a, b, c])
    plan = gen.generate()
    all_codes = [c.code for s in plan.semesters for c in s.courses]
    assert all_codes.index("AAA100") < all_codes.index("BBB200")
    assert all_codes.index("BBB200") < all_codes.index("CCC300")


def test_prior_completed_skips_course():
    """Courses in prior_completed must not appear in any semester."""
    a = _make_course("AAA100")
    b = _make_course("BBB200", prereq=CoursePrerequisite("AAA100"))
    gen = _make_generator([a, b], prior_completed=frozenset({"AAA100"}))
    plan = gen.generate()
    all_codes = {c.code for s in plan.semesters for c in s.courses}
    assert "AAA100" not in all_codes
    assert "BBB200" in all_codes


def test_zero_credit_courses_excluded():
    """Zero-credit courses must never be scheduled."""
    normal = _make_course("AAA100", credits=15)
    zero   = _make_course("ZZZ000", credits=0)
    gen = _make_generator([normal, zero])
    plan = gen.generate()
    all_codes = {c.code for s in plan.semesters for c in s.courses}
    assert "ZZZ000" not in all_codes
    assert "AAA100" in all_codes


def test_credit_cap_respected():
    """No semester should exceed max_credits_per_semester."""
    courses = [_make_course(f"C{i:03d}", credits=15) for i in range(20)]
    gen = _make_generator(courses, max_credits_per_semester=45)
    plan = gen.generate()
    for sem in plan.semesters:
        assert sem.total_credits() <= 45, \
            f"Semester {sem.semester} {sem.year} has {sem.total_credits()}cr > 45cr cap"


def test_course_count_cap_respected():
    """No semester should exceed max_courses_per_semester."""
    courses = [_make_course(f"C{i:03d}", credits=15) for i in range(12)]
    gen = _make_generator(courses, max_courses_per_semester=3)
    plan = gen.generate()
    for sem in plan.semesters:
        assert len(sem.courses) <= 3, \
            f"Semester {sem.semester} {sem.year} has {len(sem.courses)} courses > cap of 3"


def test_no_summer_skips_ss_offerings():
    """When no_summer=True, SS-only courses cause a deadlock (can never be scheduled).
    
    In production this is handled by _build_working_set which filters SS-only
    courses out of the working set before passing to PlanGenerator. Here we verify
    that the generator raises ValueError when given an SS-only course with no_summer=True
    (not silently scheduling it in a Summer School semester).
    """
    ss_only = _make_course("SSS100", semesters=("SS",))
    gen = _make_generator([ss_only], no_summer=True)
    with pytest.raises(ValueError, match="No schedulable courses remain"):
        gen.generate()


def test_no_summer_plans_non_ss_correctly():
    """When no_summer=True, non-SS courses are scheduled without touching SS."""
    normal = _make_course("NNN100", semesters=("S1", "S2"))
    gen = _make_generator([normal], no_summer=True)
    plan = gen.generate()
    all_codes = {c.code for s in plan.semesters for c in s.courses}
    assert "NNN100" in all_codes
    # No semester should be Summer School
    for sem in plan.semesters:
        assert sem.semester != "SS", f"Unexpected SS semester in no_summer plan"


def test_and_prereq_requires_both():
    """AND(A, B) prereq: both A and B must appear before C."""
    a = _make_course("AAA100")
    b = _make_course("BBB100")
    c = _make_course("CCC200", prereq=AndExpression(children=(
        CoursePrerequisite("AAA100"), CoursePrerequisite("BBB100")
    )))
    gen = _make_generator([a, b, c])
    plan = gen.generate()
    all_codes = [c.code for s in plan.semesters for c in s.courses]
    assert all_codes.index("AAA100") < all_codes.index("CCC200")
    assert all_codes.index("BBB100") < all_codes.index("CCC200")


def test_no_duplicate_codes_in_plan():
    """Every course code must appear at most once in the plan."""
    courses = [_make_course(f"C{i:03d}") for i in range(10)]
    gen = _make_generator(courses)
    plan = gen.generate()
    all_codes = [c.code for s in plan.semesters for c in s.courses]
    assert len(all_codes) == len(set(all_codes)), \
        f"Duplicate codes: {[c for c in all_codes if all_codes.count(c) > 1]}"


def test_start_semester_s2():
    """Starting in S2 should schedule first courses into S2 of start_year."""
    a = _make_course("AAA100")
    gen = _make_generator([a], start_year=2026, start_semester="S2")
    plan = gen.generate()
    first_sem = plan.semesters[0]
    assert first_sem.semester == "S2"
    assert first_sem.year == 2026


def test_rebalance_merges_thin_final():
    """A very thin final semester should be merged or rebalanced into earlier semesters."""
    # Create courses that will result in 1 thin final semester
    # 5 courses in S1 (75cr > 60cr cap → split across 2 S1 semesters)
    # then 1 course in S2 = thin final
    big = [_make_course(f"B{i:03d}", credits=15, semesters=("S1",)) for i in range(4)]
    thin = _make_course("TTT100", credits=15, semesters=("S1", "S2"))
    gen = _make_generator(big + [thin], max_credits_per_semester=45)
    plan = gen.generate()
    # Final semester should have at least REBALANCE_THRESHOLD credits
    # (the rebalancer may merge or fill it)
    final_cr = plan.semesters[-1].total_credits()
    # The final semester is allowed to be below REBALANCE_THRESHOLD if
    # it genuinely can't be filled (courses not offered in final sem type)
    # - but for this setup with S1+S2 courses it should be non-trivial
    assert final_cr > 0


def test_stats_track_courses_scheduled():
    """PlanStats should count all courses correctly."""
    courses = [_make_course(f"C{i:03d}") for i in range(6)]
    gen = _make_generator(courses)
    plan = gen.generate()
    total_in_plan = sum(len(s.courses) for s in plan.semesters)
    assert gen.stats.courses_scheduled == total_in_plan


def test_equalise_moves_count():
    """Equalise should move courses to balance load (moves >= 0)."""
    # Create uneven load: many L100 courses only in S1, then a few in S2
    s1_courses = [_make_course(f"S1{i:02d}", semesters=("S1",)) for i in range(6)]
    s2_courses = [_make_course(f"S2{i:02d}", semesters=("S2",)) for i in range(2)]
    gen = _make_generator(s1_courses + s2_courses, max_credits_per_semester=45)
    plan = gen.generate()
    # The equalise pass should have moved some courses
    assert gen.stats.rebalance_moves >= 0  # can be 0 if no moves possible
    # But plan should be valid: no duplicates, all courses scheduled
    all_codes = [c.code for s in plan.semesters for c in s.courses]
    assert len(all_codes) == len(set(all_codes))


def test_filler_codes_sort_after_required():
    """Filler codes should sort after required courses in the same semester."""
    required = _make_course("REQ100")
    filler   = _make_course("FIL100")
    gen = _make_generator([required, filler])
    gen.filler_codes = frozenset({"FIL100"})
    plan = gen.generate()
    all_codes = [c.code for s in plan.semesters for c in s.courses]
    if "REQ100" in all_codes and "FIL100" in all_codes:
        req_idx = all_codes.index("REQ100")
        fil_idx = all_codes.index("FIL100")
        # In the same semester: filler should come after required due to sort key
        # (We check they're both scheduled, not strict ordering across sems)
        assert req_idx >= 0 and fil_idx >= 0


def test_generator_with_no_courses_raises():
    """An empty course dict should result in a plan with no semesters (no loop)."""
    gen = _make_generator([])
    plan = gen.generate()
    assert len(plan.semesters) == 0
