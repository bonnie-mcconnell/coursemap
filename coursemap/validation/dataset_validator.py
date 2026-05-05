"""
Dataset validation: structural and referential integrity checks for loaded
courses and majors data.

These checks run against already-loaded domain objects. courses is a
dict[str, Course] as returned by load_courses(), and majors is a
list[dict] as returned by load_majors().

Design decisions:
- Validation accumulates all errors before raising, so a single run
  reveals the complete set of problems rather than stopping at the first.
- Cycles are detected only within the known course set.  Prerequisites
  that reference unknown codes (e.g. admission gatekeepers like 627739)
  are not treated as structural errors here; they are a data-quality issue
  flagged separately under check 1.
- The cycle check uses Kahn's algorithm (in-degree reduction) to handle
  2 766 nodes without hitting Python's recursion limit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from coursemap.domain.course import Course
from coursemap.domain.requirement_serialization import requirement_from_dict
from coursemap.domain.requirement_utils import collect_course_codes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Valid vocabulary for Offering fields after normalisation by dataset_loader
# ---------------------------------------------------------------------------

VALID_SEMESTERS: set[str] = {"S1", "S2", "SS"}
VALID_CAMPUSES:  set[str] = {"A", "D", "M", "N", "W"}
VALID_MODES:     set[str] = {"DIS", "INT", "BLK"}


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------

@dataclass
class DatasetValidationResult:
    """
    Accumulates every problem found across all checks.

    errors:   things that will cause the planner to produce wrong results.
    warnings: data quality issues that degrade output but do not break it.
    """
    errors:   list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True only when there are zero errors (warnings are permitted)."""
        return len(self.errors) == 0

    def summary(self) -> str:
        """Return a human-readable multi-line summary."""
        lines = [
            f"Validation complete: {len(self.errors)} error(s), "
            f"{len(self.warnings)} warning(s)."
        ]
        if self.errors:
            lines.append("ERRORS:")
            for e in self.errors:
                lines.append(f"  [ERROR] {e}")
        if self.warnings:
            lines.append("WARNINGS:")
            for w in self.warnings:
                lines.append(f"  [WARN]  {w}")
        return "\n".join(lines)


class DatasetValidationError(ValueError):
    """Raised by validate_dataset() when one or more errors are found."""

    def __init__(self, result: DatasetValidationResult) -> None:
        self.result = result
        super().__init__(result.summary())


# ---------------------------------------------------------------------------
# Check 1: prerequisite code references
# ---------------------------------------------------------------------------

def check_prerequisite_codes(
    courses: dict[str, Course],
    errors: list[str],
    warnings: list[str],
) -> None:
    """
    Prerequisites that reference a code which exists in the course catalogue
    must resolve to an actual course.

    Unknown-code references are split into two categories:
    - Admission-gatekeeper codes that appear on nearly every course (e.g.
      627739 = "University Entrance") are counted and reported as a single
      warning rather than thousands of individual entries.
    - Other unknown codes are individual warnings (data quality, not
      blocking errors).

    Both categories are warnings rather than errors because the planner
    already handles them correctly via prereqs_met().
    """
    known = set(courses.keys())
    unknown_counts: dict[str, int] = {}

    for course in courses.values():
        if course.prerequisites is None:
            continue
        for prereq_code in course.prerequisites.required_courses():
            if prereq_code not in known:
                unknown_counts[prereq_code] = unknown_counts.get(prereq_code, 0) + 1

    if not unknown_counts:
        return

    # Codes referenced by more than 25% of the catalogue are treated as
    # admission gatekeepers (real academic prerequisites scraped by mistake).
    common_threshold = max(1, len(courses) // 4)
    common  = {c: n for c, n in unknown_counts.items() if n >= common_threshold}
    specific = {c: n for c, n in unknown_counts.items() if n < common_threshold}

    for code, count in common.items():
        logger.debug(
            "Admission-gatekeeper code %s referenced by %d courses "
            "(treated as pre-satisfied by the planner)", code, count
        )
    if common:
        example = next(iter(common))
        warnings.append(
            f"{len(common)} admission-gatekeeper prerequisite code(s) are "
            f"referenced across the dataset (e.g. {example!r}, used on "
            f"{common[example]} courses). These are not schedulable courses "
            "and are treated as already satisfied by the planner."
        )

    for code, count in sorted(specific.items(), key=lambda x: (-x[1], x[0])):
        warnings.append(
            f"Prerequisite code {code!r} is referenced by {count} course(s) "
            "but does not exist in the course catalogue."
        )


# ---------------------------------------------------------------------------
# Check 2: prerequisite cycles
# ---------------------------------------------------------------------------

def _build_prereq_adjacency(
    courses: dict[str, Course],
) -> dict[str, set[str]]:
    """
    Build the prerequisite adjacency restricted to the known course set.

    adj[A] = {B, C} means course A requires B and C (B and C must be
    scheduled before A).  Edges to unknown codes are excluded.
    """
    known = set(courses.keys())
    return {
        code: (course.prerequisites.required_courses() & known
               if course.prerequisites else set())
        for code, course in courses.items()
    }


def check_prerequisite_cycles(
    courses: dict[str, Course],
    errors: list[str],
    warnings: list[str],
) -> None:
    """
    Detect cycles in the prerequisite graph using Kahn's algorithm.

    A cycle means a set of courses that mutually depend on each other,
    making it impossible to ever schedule any of them.  Courses in a cycle
    are permanently deadlocked: _is_prerequisite_schedulable returns False
    for any combination that includes them.

    Reports the count of trapped courses and up to five representative
    mutually-dependent pairs to guide data correction.

    Only edges within the known course set are considered.
    """
    adj = _build_prereq_adjacency(courses)

    # Kahn's algorithm: iteratively remove nodes with in-degree 0.
    # Nodes that cannot be removed are in cycles.
    in_degree: dict[str, int] = {code: 0 for code in adj}
    reverse: dict[str, list[str]] = {code: [] for code in adj}

    for node, deps in adj.items():
        for dep in deps:
            in_degree[node] += 1
            reverse[dep].append(node)

    queue = [code for code, deg in in_degree.items() if deg == 0]
    processed = 0

    while queue:
        node = queue.pop()
        processed += 1
        for dependent in reverse[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    cycle_nodes = {code for code, deg in in_degree.items() if deg > 0}

    if not cycle_nodes:
        return

    # Find representative mutual-dependency pairs (A requires B and B requires A).
    pairs: list[tuple[str, str]] = []
    seen_in_pairs: set[str] = set()
    for code in sorted(cycle_nodes):
        if len(pairs) >= 5:
            break
        if code in seen_in_pairs:
            continue
        for dep in sorted(adj[code] & cycle_nodes):
            if code in adj.get(dep, set()):
                pairs.append((code, dep))
                seen_in_pairs.update([code, dep])
                break

    pair_str = ", ".join(f"{a}↔{b}" for a, b in pairs)
    remainder = len(cycle_nodes) - len(seen_in_pairs)
    suffix = f" (plus {remainder} more)" if remainder > 0 else ""

    errors.append(
        f"{len(cycle_nodes)} course(s) are trapped in prerequisite cycles "
        f"and can never be scheduled. "
        f"Representative mutual dependencies: {pair_str}{suffix}. "
        "These courses will be excluded by _is_prerequisite_schedulable."
    )


# ---------------------------------------------------------------------------
# Check 3: credit validity
# ---------------------------------------------------------------------------

def check_credits(
    courses: dict[str, Course],
    errors: list[str],
    warnings: list[str],
) -> None:
    """Credits must be a positive integer."""
    for code, course in courses.items():
        if not isinstance(course.credits, int):
            errors.append(
                f"Course {code!r}: credits must be an int, "
                f"got {type(course.credits).__name__!r} ({course.credits!r})."
            )
        elif course.credits == 0:
            # Zero-credit courses (practicums, language enrolments, etc.) are
            # intentionally non-schedulable - downgrade from error to warning.
            warnings.append(
                f"Course {code!r}: credits=0 (non-schedulable practicum/language course - will be skipped by planner)."
            )
        elif course.credits < 0:
            errors.append(
                f"Course {code!r}: credits must be non-negative, got {course.credits}."
            )


# ---------------------------------------------------------------------------
# Check 4: offering validity
# ---------------------------------------------------------------------------

def check_offerings(
    courses: dict[str, Course],
    errors: list[str],
    warnings: list[str],
) -> None:
    """
    Each offering must have a recognised semester, campus, and mode code.

    Courses with no offerings at all are warned rather than errored: they
    exist legitimately in the dataset (e.g. discontinued courses) and the
    planner will simply never schedule them.
    """
    for code, course in courses.items():
        if not course.offerings:
            warnings.append(
                f"Course {code!r} ({course.title!r}) has no offerings "
                "and cannot be scheduled by the planner."
            )
            continue

        for i, offering in enumerate(course.offerings):
            if offering.semester not in VALID_SEMESTERS:
                errors.append(
                    f"Course {code!r} offering[{i}]: unrecognised semester "
                    f"{offering.semester!r}. "
                    f"Valid values: {sorted(VALID_SEMESTERS)}."
                )
            if offering.campus not in VALID_CAMPUSES:
                errors.append(
                    f"Course {code!r} offering[{i}]: unrecognised campus "
                    f"{offering.campus!r}. "
                    f"Valid values: {sorted(VALID_CAMPUSES)}."
                )
            if offering.mode not in VALID_MODES:
                errors.append(
                    f"Course {code!r} offering[{i}]: unrecognised mode "
                    f"{offering.mode!r}. "
                    f"Valid values: {sorted(VALID_MODES)}."
                )


# ---------------------------------------------------------------------------
# Check 5: major course code references
# ---------------------------------------------------------------------------

def check_major_course_codes(
    courses: dict[str, Course],
    majors: list[dict],
    errors: list[str],
    warnings: list[str],
) -> None:
    """
    Every course code referenced in a major's requirement tree must exist
    in the course catalogue.

    Missing codes cause the solver to silently skip those courses
    (_build_course_subset drops codes not in self.courses), producing plans
    that do not actually include all courses the major intended.  This is
    reported as a warning rather than an error because the planner continues
    to function; it simply produces incomplete plans for affected majors.

    A requirement tree that fails to parse is always an error.
    """
    known = set(courses.keys())

    for major in majors:
        name = major.get("name", "<unnamed>")
        req_dict = major.get("requirement")

        if not req_dict:
            warnings.append(f"Major {name!r} has no requirement tree.")
            continue

        try:
            req_node = requirement_from_dict(req_dict)
        except Exception as exc:
            errors.append(
                f"Major {name!r}: requirement tree failed to parse: {exc}"
            )
            continue

        referenced = collect_course_codes(req_node)
        missing = sorted(referenced - known)

        if missing:
            preview = missing[:10]
            tail = f" … and {len(missing) - 10} more" if len(missing) > 10 else ""
            warnings.append(
                f"Major {name!r} references {len(missing)} course code(s) "
                f"not in the catalogue: {preview}{tail}."
            )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_dataset(
    courses: dict[str, Course],
    majors: list[dict],
    *,
    raise_on_error: bool = True,
) -> DatasetValidationResult:
    """
    Run all structural and referential integrity checks against the loaded
    dataset.

    Checks performed (in order):
        1. All course codes referenced in prerequisites exist.
        2. No prerequisite cycles exist within the known course set.
        3. Credits are valid positive integers.
        4. Offering semester, campus, and mode values are recognised.
        5. All course codes referenced in major requirement trees exist.

    Args:
        courses:        dict[str, Course] as returned by load_courses().
        majors:         list[dict] as returned by load_majors().
        raise_on_error: If True (default), raise DatasetValidationError when
                        any errors are found.  Pass False to inspect the full
                        result without raising.

    Returns:
        DatasetValidationResult containing all accumulated errors and warnings.

    Raises:
        DatasetValidationError: when raise_on_error is True and errors exist.
    """
    errors:   list[str] = []
    warnings: list[str] = []

    logger.info(
        "Validating dataset: %d courses, %d majors", len(courses), len(majors)
    )

    check_prerequisite_codes(courses, errors, warnings)
    check_prerequisite_cycles(courses, errors, warnings)
    check_credits(courses, errors, warnings)
    check_offerings(courses, errors, warnings)
    check_major_course_codes(courses, majors, errors, warnings)

    result = DatasetValidationResult(errors=errors, warnings=warnings)
    logger.info(result.summary().splitlines()[0])

    if raise_on_error and not result.is_valid:
        raise DatasetValidationError(result)

    return result
