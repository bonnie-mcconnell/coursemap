"""Degree plan types: SemesterPlan and DegreePlan."""
from __future__ import annotations

from dataclasses import dataclass, field

from .course import Course


@dataclass(frozen=True)
class SemesterPlan:
    """One semester's worth of scheduled courses."""

    year:     int
    semester: str
    courses:  tuple[Course, ...]

    def total_credits(self) -> int:
        return sum(course.credits for course in self.courses)


@dataclass
class DegreePlan:
    """
    A complete degree plan as returned by PlanGenerator.

    semesters contains only the newly-scheduled semesters. prior_completed holds
    courses finished before this plan was generated (passed via --completed).
    transfer_credits is raw credit recognition from prior learning at another
    institution (passed via --transfer-credits), counted toward the degree total
    but not tied to specific course codes.
    Both contribute to the degree total but prior courses do not appear in the
    semester output.

    all_course_codes is computed eagerly in __post_init__ from the immutable
    semesters and prior_completed tuples. This avoids the cached_property pattern
    on a mutable dataclass, where reassigning semesters after first access would
    silently return stale data.
    """

    semesters:        tuple[SemesterPlan, ...]
    prior_completed:  tuple[Course, ...] = field(default_factory=tuple)
    transfer_credits: int = 0

    # Computed eagerly; treated as read-only after construction.
    all_course_codes: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        scheduled = frozenset(
            course.code
            for semester in self.semesters
            for course in semester.courses
        )
        object.__setattr__(self, "all_course_codes",
                           scheduled | frozenset(c.code for c in self.prior_completed))

    def __setattr__(self, name: str, value) -> None:
        """
        Guard against post-construction mutation that would leave all_course_codes stale.

        DegreePlan is a mutable dataclass (not frozen=True) because plan_store
        needs to attach metadata after construction. This guard prevents silent
        correctness bugs from code that reassigns semesters or prior_completed
        without recomputing all_course_codes.
        """
        if name in ("semesters", "prior_completed") and hasattr(self, "all_course_codes"):
            raise AttributeError(
                f"Cannot reassign DegreePlan.{name} after construction - "
                "all_course_codes would become stale. "
                "Create a new DegreePlan instance instead."
            )
        object.__setattr__(self, name, value)

    def total_credits(self) -> int:
        """Credits earned in newly-scheduled semesters (excludes prior and transfer)."""
        return sum(semester.total_credits() for semester in self.semesters)

    def prior_credits(self) -> int:
        """Credits from courses completed before this plan (via --completed)."""
        return sum(c.credits for c in self.prior_completed)

    def all_prior_credits(self) -> int:
        """Total credits recognised before this plan: completed courses + transfer."""
        return self.prior_credits() + self.transfer_credits
