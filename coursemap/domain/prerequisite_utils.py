"""
Utility helpers for evaluating prerequisite expressions in a scheduling context.
"""

from __future__ import annotations
from coursemap.domain.prerequisite import (
    AndExpression,
    CoursePrerequisite,
    OrExpression,
    PrerequisiteExpression,
)


def prereqs_met(
    prereq: PrerequisiteExpression | None,
    completed: set[str],
    known: set[str],
) -> bool:
    """
    Return True if prereq is satisfied given the completed and known sets.

    A prerequisite code that is not in known (i.e. not one of the courses
    being scheduled) is treated as already satisfied.  This handles admission
    gatekeepers such as University Entrance codes that the scraper captures as
    prerequisites but that never appear as schedulable courses.

    Args:
        prereq:    The prerequisite expression to evaluate, or None.
        completed: Course codes that have already been scheduled.
        known:     Course codes that are in scope for this planning run.
                   Any prerequisite code absent from known is treated as
                   satisfied (i.e. assumed to be met outside this plan).

    Note: this function cannot distinguish "absent from known because it's
    a genuine external gatekeeper" from "absent from known because the
    working-set builder chose a different OR-sibling branch instead" - both
    look identical from here. The working-set builder (PlanSearch.
    _build_working_set) is responsible for ensuring an OR's chosen branch is
    actually added to `known` in a way that this function's straightforward
    per-leaf rule produces the right answer; see its OR-aware expansion
    comment for the related ordering bug this used to cause and how it's
    actually fixed (by making sure the rebalancer's _prereq_codes_or_aware
    correctly identifies the chosen branch as required, not by changing this
    function's contract).
    """
    if prereq is None:
        return True
    if isinstance(prereq, CoursePrerequisite):
        return prereq.code not in known or prereq.code in completed
    if isinstance(prereq, (AndExpression, OrExpression)):
        fn = all if isinstance(prereq, AndExpression) else any
        return fn(prereqs_met(child, completed, known) for child in prereq.children)
    # Fallback for any future PrerequisiteExpression subclass.
    # KNOWN LIMITATION: required_courses() returns the union of all OR branches,
    # so this check is too permissive for OR expressions - it may return True
    # when only some OR branches are satisfied via out-of-scope codes.
    # This fallback only fires for unknown subclasses; in practice it is dead code.
    return prereq.is_satisfied(completed | (prereq.required_courses() - known))
