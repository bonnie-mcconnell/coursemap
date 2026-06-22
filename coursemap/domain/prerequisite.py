"""
Prerequisite expression tree.

Prerequisite logic for a course is represented as a tree of
PrerequisiteExpression nodes. The two compound forms are:

  AndExpression  -- all listed courses must be completed first
  OrExpression   -- at least one listed course must be completed first

The scheduler evaluates these trees via prereqs_met() in prerequisite_utils.py
rather than calling is_satisfied() directly, because the scheduler treats
out-of-scope prerequisites as pre-satisfied (admission gatekeepers, external
courses from a prior degree, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import reduce


class PrerequisiteExpression(ABC):

    @abstractmethod
    def is_satisfied(self, completed: set[str]) -> bool:
        ...

    @abstractmethod
    def required_courses(self) -> set[str]:
        ...


@dataclass(frozen=True)
class CoursePrerequisite(PrerequisiteExpression):
    """A single required course prerequisite."""

    code: str

    def is_satisfied(self, completed: set[str]) -> bool:
        return self.code in completed

    def required_courses(self) -> set[str]:
        return {self.code}


@dataclass(frozen=True)
class AndExpression(PrerequisiteExpression):
    """All child prerequisites must be satisfied."""

    children: tuple[PrerequisiteExpression, ...]

    def is_satisfied(self, completed: set[str]) -> bool:
        return all(child.is_satisfied(completed) for child in self.children)

    def required_courses(self) -> set[str]:
        return reduce(set.__or__, (c.required_courses() for c in self.children), set())


@dataclass(frozen=True)
class OrExpression(PrerequisiteExpression):
    """At least one child prerequisite must be satisfied."""

    children: tuple[PrerequisiteExpression, ...]

    def is_satisfied(self, completed: set[str]) -> bool:
        return any(child.is_satisfied(completed) for child in self.children)

    def required_courses(self) -> set[str]:
        """
        Return ALL codes mentioned in any branch (union).

        WARNING: this is NOT "all codes that must be completed" - for an OR
        expression, only ONE branch needs to be satisfied. Callers that need
        "which codes are truly required" should use _prereq_codes_or_aware()
        from planner/generator.py or ElectiveFiller._prereqs_satisfiable(),
        both of which return the intersection of OR branches.

        This method returns the full union for use cases that genuinely need
        all mentioned codes (e.g. building a complete prerequisite graph for
        display, or checking if a code appears anywhere in the expression).
        """
        return reduce(set.__or__, (c.required_courses() for c in self.children), set())


# ---------------------------------------------------------------------------
# Serialisation helpers (used by API and CLI to avoid duplication)
# ---------------------------------------------------------------------------

def prereq_to_dict(expr: "PrerequisiteExpression | None") -> "dict | None":
    """Serialise a PrerequisiteExpression tree to a JSON-friendly dict."""
    if expr is None:
        return None
    if isinstance(expr, CoursePrerequisite):
        return {"type": "course", "code": expr.code}
    if isinstance(expr, AndExpression):
        return {"type": "and", "children": [prereq_to_dict(c) for c in expr.children]}
    if isinstance(expr, OrExpression):
        return {"type": "or", "children": [prereq_to_dict(c) for c in expr.children]}
    return {"type": "unknown", "repr": str(expr)}


def prereq_to_human(expr: "PrerequisiteExpression | None") -> "str | None":
    """Render a PrerequisiteExpression as a readable string."""
    if expr is None:
        return None
    if isinstance(expr, CoursePrerequisite):
        return expr.code
    if isinstance(expr, AndExpression):
        parts = [prereq_to_human(c) for c in expr.children]
        inner = " AND ".join(p for p in parts if p)
        return f"({inner})" if len(parts) > 1 else inner
    if isinstance(expr, OrExpression):
        parts = [prereq_to_human(c) for c in expr.children]
        inner = " OR ".join(p for p in parts if p)
        return f"({inner})" if len(parts) > 1 else inner
    return str(expr)
