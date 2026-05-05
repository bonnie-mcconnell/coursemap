"""Course and offering domain types."""
from __future__ import annotations
from dataclasses import dataclass, field

from .prerequisite import PrerequisiteExpression


@dataclass(frozen=True)
class Offering:
    """One available instance of a course in a specific semester and delivery mode."""
    semester: str   # "S1", "S2", or "SS" (Summer School)
    campus: str     # "D" (distance), "M" (Manawatu), "A" (Auckland), "W" (Wellington)
    mode: str       # "DIS" (distance), "INT" (internal), "BLK" (block)


@dataclass(frozen=True)
class Course:
    """
    A Massey course as loaded from the dataset.

    corequisites: codes of courses that must be taken in the same semester or
    already completed before enrolment. Satisfied when the code is in the
    completed set or co-enrolled in the current semester.

    restrictions: codes of courses that permanently block enrolment in this
    course once completed. If any restriction code is in the completed set,
    this course cannot be scheduled for the rest of the plan.
    """
    code:          str
    title:         str
    credits:       int
    level:         int
    offerings:     tuple[Offering, ...]
    prerequisites: PrerequisiteExpression | None = None
    corequisites:  frozenset[str] = field(default_factory=frozenset)
    restrictions:  frozenset[str] = field(default_factory=frozenset)
    url:           str | None = None          # Massey catalogue URL for this course
    description:   str | None = None          # Short description (intro field from scraper)

    def is_offered(self, semester: str, campus: str, mode: str) -> bool:
        """Return True if this course has an offering in the given semester and delivery mode."""
        return any(
            o.semester == semester and o.campus == campus and o.mode == mode
            for o in self.offerings
        )
