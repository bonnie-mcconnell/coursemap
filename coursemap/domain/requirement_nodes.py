"""
Degree requirement tree nodes as described in docs/03_requirement_language.md.
Each node implements is_satisfied(plan) to evaluate against a degree plan.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from .plan import DegreePlan


class RequirementNode(ABC):
    """Base type for all degree requirement tree nodes."""

    @abstractmethod
    def is_satisfied(self, plan: DegreePlan) -> bool:
        """Return True if this requirement is satisfied by the given plan."""
        ...


@dataclass(frozen=True)
class CourseRequirement(RequirementNode):
    """Represents a required course."""

    course_code: str

    def is_satisfied(self, plan: DegreePlan) -> bool:
        return self.course_code in plan.all_course_codes


@dataclass(frozen=True)
class AllOfRequirement(RequirementNode):
    """All child requirements must be satisfied."""

    children: tuple[RequirementNode, ...]

    def is_satisfied(self, plan: DegreePlan) -> bool:
        return all(child.is_satisfied(plan) for child in self.children)


@dataclass(frozen=True)
class AnyOfRequirement(RequirementNode):
    """At least one child requirement must be satisfied."""

    children: tuple[RequirementNode, ...]

    def is_satisfied(self, plan: DegreePlan) -> bool:
        return any(child.is_satisfied(plan) for child in self.children)


@dataclass(frozen=True)
class ChooseCreditsRequirement(RequirementNode):
    """
    Choose courses totaling a specified credit amount from a list.

    When ``course_codes`` is empty AND ``open_pool=True``, this represents an
    unrestricted free-elective requirement (e.g. "choose any 150cr of Massey
    courses") rather than an impossible-to-satisfy pool with zero options.
    This distinction matters: an empty `course_codes` tuple is ambiguous on
    its own (it could mean "no eligible courses exist" or "any course is
    eligible"), so `open_pool` makes the intended meaning explicit instead of
    relying on callers to infer it from context.

    For an open pool, satisfaction is judged by counting credits from any
    course in the plan that isn't already claimed by a more specific
    requirement elsewhere in the tree. Since this node has no way to know
    what else the tree requires, `is_satisfied` approximates by counting
    credits from courses NOT in `excluded_codes` (courses already counted by
    other parts of the tree) - set by the caller before checking.
    """

    credits: int
    course_codes: tuple[str, ...]
    open_pool: bool = False
    excluded_codes: frozenset[str] = field(default_factory=frozenset)

    def is_satisfied(self, plan: DegreePlan) -> bool:
        if self.credits <= 0:
            return True

        if self.open_pool and not self.course_codes:
            claimed = self.excluded_codes
            total = sum(
                course.credits
                for semester in plan.semesters
                for course in semester.courses
                if course.code not in claimed
            )
            total += sum(
                c.credits for c in plan.prior_completed
                if c.code not in claimed
            )
            return total >= self.credits

        allowed = set(self.course_codes)
        total = sum(
            course.credits
            for semester in plan.semesters
            for course in semester.courses
            if course.code in allowed
        )
        # Also count courses completed before this plan was generated.
        total += sum(
            c.credits for c in plan.prior_completed
            if c.code in allowed
        )
        return total >= self.credits


@dataclass(frozen=True)
class ChooseNRequirement(RequirementNode):
    """Choose N courses from a list."""

    n: int
    course_codes: tuple[str, ...]

    def is_satisfied(self, plan: DegreePlan) -> bool:
        plan_codes = plan.all_course_codes
        chosen = sum(1 for code in self.course_codes if code in plan_codes)
        return chosen >= self.n


@dataclass(frozen=True)
class MinLevelCreditsRequirement(RequirementNode):
    """Minimum credits from courses at a specific level."""

    level: int
    min_credits: int

    def is_satisfied(self, plan: DegreePlan) -> bool:
        total = 0
        for semester in plan.semesters:
            for course in semester.courses:
                if course.level == self.level:
                    total += course.credits
        # Also count prior-completed courses at this level
        total += sum(
            c.credits for c in plan.prior_completed if c.level == self.level
        )
        return total >= self.min_credits


@dataclass(frozen=True)
class MinLevelCreditsFromRequirement(RequirementNode):
    """Minimum credits at a specific level from a given set of courses."""

    level: int
    min_credits: int
    course_codes: tuple[str, ...]

    def is_satisfied(self, plan: DegreePlan) -> bool:
        allowed = set(self.course_codes)
        total = sum(
            course.credits
            for semester in plan.semesters
            for course in semester.courses
            if course.code in allowed and course.level == self.level
        )
        # Also count prior-completed courses from the allowed set at this level
        total += sum(
            c.credits for c in plan.prior_completed
            if c.code in allowed and c.level == self.level
        )
        return total >= self.min_credits


@dataclass(frozen=True)
class MaxLevelCreditsRequirement(RequirementNode):
    """Maximum credits from courses at a specific level."""

    level: int
    max_credits: int

    def is_satisfied(self, plan: DegreePlan) -> bool:
        total = 0
        for semester in plan.semesters:
            for course in semester.courses:
                if course.level == self.level:
                    total += course.credits
        # Also count prior-completed courses at this level
        total += sum(
            c.credits for c in plan.prior_completed if c.level == self.level
        )
        return total <= self.max_credits


@dataclass(frozen=True)
class TotalCreditsRequirement(RequirementNode):
    """Total credits required for the degree."""

    required_credits: int

    def is_satisfied(self, plan: DegreePlan) -> bool:
        # Include transfer credits (prior learning) in the total
        return plan.total_credits() + plan.all_prior_credits() >= self.required_credits


@dataclass(frozen=True)
class MajorRequirement(RequirementNode):
    """Requirement representing a major program (subtree)."""

    name: str
    requirement: RequirementNode

    def is_satisfied(self, plan: DegreePlan) -> bool:
        return self.requirement.is_satisfied(plan)
