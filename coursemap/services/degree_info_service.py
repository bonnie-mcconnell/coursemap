"""
DegreeInfoService - thin wrapper that extracts degree-level queries
from PlannerService into a focused, independently testable module.

All methods delegate to the underlying PlannerService; this class
exists to give callers a narrower interface for read-only degree
metadata queries (credits, required codes, gap, validation).
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .planner_service import PlannerService


class DegreeInfoService:
    """
    Read-only queries about degree structure and credit requirements.

    Injected with a reference to the main PlannerService so it can
    access the shared course catalogue and qualification map without
    duplicating data.
    """

    def __init__(self, svc: "PlannerService") -> None:
        self._svc = svc

    # ── Credit totals ────────────────────────────────────────────────────────

    def degree_total_credits(self, major_name: str) -> int:
        """Total credits required by the degree programme (e.g. 360 for a bachelor's)."""
        return self._svc.degree_total_credits(major_name)

    def free_elective_gap(
        self,
        major_name: str,
        campus: str = "D",
        mode: str = "DIS",
    ) -> int:
        """
        Credits NOT covered by named major requirements that the student
        must fill with free electives (or a second major).
        """
        return self._svc.free_elective_gap(major_name, campus=campus, mode=mode)

    # ── Required courses ─────────────────────────────────────────────────────

    def required_codes(self, major_name: str) -> set[str]:
        """Set of course codes that are explicitly required by this major."""
        return self._svc.required_course_codes(major_name)

    def excluded_by_campus(
        self,
        major_name: str,
        campus: str,
        mode: str,
    ) -> set[str]:
        """
        Required courses that are not offered in the requested campus/mode
        combination - these will appear as warnings on any generated plan.
        """
        return self._svc.campus_excluded_courses(major_name, campus=campus, mode=mode)

    # ── Prerequisite coverage ────────────────────────────────────────────────

    def prereq_coverage(self, course_codes: list[str]) -> dict:
        """
        For a list of course codes, return coverage statistics:
          - total: number of courses
          - with_prereq_data: courses where prerequisite data is present
          - coverage_pct: percentage (0–100)
          - missing: codes with no prereq data (inferred or absent)
        """
        courses = self._svc.courses
        total = len(course_codes)
        with_data = sum(
            1 for c in course_codes
            if c in courses and courses[c].prerequisites is not None
        )
        missing = [
            c for c in course_codes
            if c in courses and courses[c].prerequisites is None
            and courses[c].level >= 200  # only meaningful for L200+
        ]
        return {
            "total": total,
            "with_prereq_data": with_data,
            "coverage_pct": round(100 * with_data / total) if total else 100,
            "missing_prereq_data": missing,
        }
