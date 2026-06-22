"""
ElectiveFiller - subject-area elective recommendation engine.

Extracted from PlannerService.generate_filled_plan() to:
  1. Remove the ~400-line monolith from planner_service.py
  2. Allow the same logic to be called identically for single and double majors
  3. Make the fill strategy independently testable

Usage::

    filler = ElectiveFiller(courses, campus="D", mode="DIS")
    candidates = filler.rank_candidates(
        seed_codes=["159101", "159201"],   # courses already in plan
        exclude=frozenset(["159999"]),
        prefer=frozenset(["159234"]),
        budget_credits=60,
        completed=frozenset(),
    )
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from coursemap.domain.course import Course

logger = logging.getLogger(__name__)


@dataclass
class FillerCandidate:
    code: str
    credits: int
    level: int
    prefix: str          # subject-area prefix (first 3 digits of code)
    tier: int            # lower = better (0=preferred, 1=same-area, 2=adjacent)
    is_preferred: bool = False


class ElectiveFiller:
    """
    Ranks elective candidates to fill a free-elective credit gap.

    Strategy (in priority order):
      Tier 0 - explicitly preferred courses (student-supplied ``prefer`` list)
      Tier 1 - courses whose 3-digit prefix matches the most common prefix
                in the plan (same subject area), at the lowest available level
      Tier 2 - courses from subject areas with at least one course already in
                the plan but not in Tier 1 (adjacent subject areas)

    Within each tier, courses are sorted by (level ASC, code ASC) so earlier,
    lower-level courses are always preferred. This mimics the order a real
    academic advisor would suggest: "take lower-level electives first so you
    can build on them."

    The filler never recommends:
      - Zero-credit courses (practicums, language enrolments)
      - Courses with no offerings in the student's campus/mode
      - Courses already in the plan or in the exclude set
      - Courses whose prerequisites are not fully met by the current plan
    """

    def __init__(
        self,
        all_courses: dict[str, Course],
        campus: str = "D",
        mode: str = "DIS",
    ) -> None:
        self.courses = all_courses
        self.campus = campus
        self.mode = mode

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rank_candidates(
        self,
        seed_codes: list[str],
        completed: frozenset[str],
        budget_credits: int,
        exclude: frozenset[str] = frozenset(),
        prefer: frozenset[str] = frozenset(),
        level_cap: int | None = None,
    ) -> list[FillerCandidate]:
        """
        Return a ranked list of elective candidates.

        Args:
            seed_codes:     Course codes already in the plan (used to derive
                            the subject-area prefix ranking).
            completed:      Codes the student has already completed (satisfy prereqs).
            budget_credits: Maximum total credits needed from fillers.
            exclude:        Codes to never recommend (excluded by student or major).
            prefer:         Codes the student explicitly wants prioritised.
            level_cap:      If set, exclude courses at this level or above
                            (e.g. 400 to avoid honours-only courses).

        Returns:
            List of FillerCandidate, best-first. The caller is responsible for
            selecting enough candidates to fill the budget.
        """
        # Derive subject prefix distribution from seed codes
        prefix_counts = Counter(
            self._prefix(c) for c in seed_codes
            if c in self.courses and self._prefix(c)
        )
        top_prefix = prefix_counts.most_common(1)[0][0] if prefix_counts else ""
        secondary_prefixes = {p for p, _ in prefix_counts.most_common(5) if p != top_prefix}

        already_in_plan = set(seed_codes) | set(completed) | exclude

        candidates: list[FillerCandidate] = []

        for code, course in self.courses.items():
            if code in already_in_plan:
                continue
            if course.credits <= 0:
                continue
            if not self._is_offered(course):
                continue
            if level_cap and course.level >= level_cap:
                continue
            if not self._prereqs_satisfiable(course, completed | set(seed_codes)):
                continue

            prefix = self._prefix(code)
            is_preferred = code in prefer

            if is_preferred:
                tier = 0
            elif prefix == top_prefix:
                tier = 1
            elif prefix in secondary_prefixes:
                tier = 2
            elif not top_prefix:
                # No seed codes provided - broad-search mode: include everything
                tier = 3
            else:
                continue  # out of scope - don't recommend

            candidates.append(FillerCandidate(
                code=code,
                credits=course.credits,
                level=course.level,
                prefix=prefix,
                tier=tier,
                is_preferred=is_preferred,
            ))

        # Sort: tier ASC, level ASC, code ASC
        candidates.sort(key=lambda c: (c.tier, c.level, c.code))

        logger.debug(
            "ElectiveFiller: %d candidates for budget=%dcr (top prefix=%s)",
            len(candidates), budget_credits, top_prefix,
        )
        return candidates

    def select_to_fill(
        self,
        seed_codes: list[str],
        completed: frozenset[str],
        budget_credits: int,
        exclude: frozenset[str] = frozenset(),
        prefer: frozenset[str] = frozenset(),
        level_cap: int | None = None,
    ) -> list[str]:
        """
        Select elective codes that fit within budget_credits.

        Greedy selection: takes candidates in rank order until budget is filled.
        Returns just the course codes (not FillerCandidate objects).
        """
        ranked = self.rank_candidates(
            seed_codes=seed_codes,
            completed=completed,
            budget_credits=budget_credits,
            exclude=exclude,
            prefer=prefer,
            level_cap=level_cap,
        )

        selected: list[str] = []
        remaining = budget_credits
        for cand in ranked:
            if remaining <= 0:
                break
            if cand.credits <= remaining:
                selected.append(cand.code)
                remaining -= cand.credits

        logger.debug(
            "ElectiveFiller.select_to_fill: selected %d courses for %dcr budget "
            "(remaining=%dcr)",
            len(selected), budget_credits, remaining,
        )
        return selected

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _prefix(code: str) -> str:
        """Return the 3-digit numeric prefix of a course code."""
        digits = "".join(c for c in code if c.isdigit())
        return digits[:3] if len(digits) >= 3 else ""

    def _is_offered(self, course: Course) -> bool:
        """Return True if the course has any offering on the student's campus/mode."""
        for o in (course.offerings or []):
            o_campus = getattr(o, "campus_code", getattr(o, "campus", ""))
            o_mode   = getattr(o, "delivery_mode", getattr(o, "mode", ""))
            if (not self.campus or o_campus == self.campus) and \
               (not self.mode   or o_mode   == self.mode):
                return True
        return False

    @staticmethod
    def _prereqs_satisfiable(course: Course, available: set[str]) -> bool:
        """
        Return True if the course's prerequisites can be met from ``available``.

        Uses an OR-aware check: an OR expression is satisfiable if ANY one branch
        is met. An AND expression requires ALL branches. This prevents the old
        ``required_courses()`` approach from demanding all OR-branch codes are
        present simultaneously (which is too strict - it rejects courses that
        only need one of several alternatives).
        """
        prereq = course.prerequisites
        if prereq is None:
            return True

        from coursemap.domain.prerequisite import (
            AndExpression,
            OrExpression,
            CoursePrerequisite,
        )

        def _sat(node) -> bool:
            if isinstance(node, CoursePrerequisite):
                return node.code in available
            if isinstance(node, AndExpression):
                return all(_sat(c) for c in node.children)
            if isinstance(node, OrExpression):
                return any(_sat(c) for c in node.children)
            # Unknown node type: assume satisfiable (don't over-reject)
            return True

        return _sat(prereq)
