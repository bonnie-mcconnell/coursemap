"""
Dataset loaders: read courses.json and majors.json into domain objects.

Public API:
  load_courses()   -- returns dict[str, Course]
  load_majors()    -- returns list[dict]
  normalize_campus(), normalize_mode(), sanitize_course_code(), parse_offerings()
    -- normalisation helpers, also used by tests.

Internal helpers (_parse_prereqs, _is_plausible_prereq, _build_expr_from_struct,
_build_major_requirement_dict, _parse_code_set) are implementation details of
the JSON-to-domain transformation and not part of the public API.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from coursemap.domain.prerequisite import (
    CoursePrerequisite,
    AndExpression,
    OrExpression,
    PrerequisiteExpression,
)
from coursemap.domain.course import Course, Offering

logger = logging.getLogger(__name__)

# Codes that appear in nearly every course's prerequisite list because the
# original scraper grabbed all six-digit codes visible on the page, including
# ones in the page header rather than the academic prerequisites section.
# These are admission conditions (University Entrance, NZQF Level 3 markers)
# that are never schedulable courses. Stripping them leaves the actual
# academic prerequisites intact.
_ADMISSION_NOISE_CODES = frozenset({
    "627739",  # University Entrance marker (appears on every Massey page header)
    "219206",  # NZQF Level 3 marker      (appears on every Massey page header)
})


# Anchor dataset paths to the repository root so the loader works regardless
# of the working directory when the CLI is invoked.
#
# Repository layout:
#   <repo>/
#     datasets/           <- all JSON datasets live here
#     coursemap/          <- Python package root
#       ingestion/
#         dataset_loader.py
#
# parents[0] = ingestion/, parents[1] = coursemap/, parents[2] = <repo>/
_DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"

DATASET_PATH       = _DATASETS_DIR / "courses.json"
MAJORS_DATASET_PATH = _DATASETS_DIR / "majors.json"

# Maps raw scraper semester strings to planner codes.
# "Double Semester" and "Full Year" expand to two offerings (S1 + S2)
# and are handled separately in parse_offerings.
_SEMESTER_MAP: dict[str, str] = {
    "Semester 1": "S1",
    "Semester 2": "S2",
    "Summer School": "SS",
}

_MULTI_SEMESTER_MAP: dict[str, list[str]] = {
    "Double Semester": ["S1", "S2"],
    "Full Year": ["S1", "S2"],
}

# Maps raw campus codes from the scraper to the canonical planner campus code.
# All known campus codes map to themselves; unknown values fall back to "D".
_CAMPUS_MAP: dict[str, str] = {
    "A": "A",   # Auckland
    "D": "D",   # Distance / online
    "M": "M",   # Manawatū (Palmerston North)
    "N": "N",   # NZCM
    "W": "W",   # Wellington
}
_CAMPUS_FALLBACK = "D"

# Maps raw delivery mode codes from the scraper to the canonical planner mode.
# Unknown values fall back to "DIS" (distance).
_MODE_MAP: dict[str, str] = {
    "DIS": "DIS",   # Distance / online
    "INT": "INT",   # Internal (on-campus)
    "BLK": "BLK",   # Block (intensive)
}
_MODE_FALLBACK = "DIS"


# ---------------------------------------------------------------------------
# Dataset schema validators
# ---------------------------------------------------------------------------

def _validate_courses_schema(raw: Any) -> None:
    """
    Raise ValueError with a clear message if courses.json has an unexpected shape.

    Checks performed (fast, not exhaustive):
    - Top-level must be a list.
    - At least one entry must exist.
    - Each entry must be a dict with 'course_code' and 'title' keys.
    - First entry must have numeric (or null) 'credits'.
    """
    if not isinstance(raw, list):
        raise ValueError(
            f"courses.json: expected a JSON array at the top level, "
            f"got {type(raw).__name__}. Re-run ingestion to regenerate."
        )
    if len(raw) == 0:
        raise ValueError("courses.json: array is empty - dataset may be corrupted.")
    required_keys = {"course_code", "title"}
    for i, entry in enumerate(raw[:5]):   # sample first 5 entries
        if not isinstance(entry, dict):
            raise ValueError(
                f"courses.json: entry {i} is {type(entry).__name__}, expected dict."
            )
        missing = required_keys - entry.keys()
        if missing:
            raise ValueError(
                f"courses.json: entry {i} is missing key(s): {sorted(missing)}. "
                "Re-run ingestion to regenerate."
            )
        credits = entry.get("credits")
        if credits is not None and not isinstance(credits, (int, float)):
            raise ValueError(
                f"courses.json: entry {i} ({entry.get('course_code', '?')!r}) "
                f"has non-numeric 'credits': {credits!r}."
            )


def _validate_majors_schema(raw: Any) -> None:
    """
    Raise ValueError with a clear message if majors.json has an unexpected shape.

    Accepts both list format and dict format (handled by load_majors).
    """
    if not isinstance(raw, (list, dict)):
        raise ValueError(
            f"majors.json: expected a JSON array or object, "
            f"got {type(raw).__name__}. Re-run ingestion to regenerate."
        )
    if isinstance(raw, list):
        if len(raw) == 0:
            raise ValueError("majors.json: array is empty - dataset may be corrupted.")
        for i, entry in enumerate(raw[:3]):
            if not isinstance(entry, dict):
                raise ValueError(
                    f"majors.json: entry {i} is {type(entry).__name__}, expected dict."
                )
            if "name" not in entry:
                raise ValueError(
                    f"majors.json: entry {i} is missing 'name' key. "
                    "Re-run ingestion to regenerate."
                )
    else:  # dict format
        if len(raw) == 0:
            raise ValueError("majors.json: object is empty - dataset may be corrupted.")


def normalize_campus(raw: str) -> str:
    """Return the canonical campus code, falling back to 'D' for unknowns."""
    return _CAMPUS_MAP.get(raw, _CAMPUS_FALLBACK)


def normalize_mode(raw: str) -> str:
    """Return the canonical delivery mode, falling back to 'DIS' for unknowns."""
    return _MODE_MAP.get(raw.upper().strip(), _MODE_FALLBACK)


def sanitize_course_code(raw: str) -> str:
    """
    Return a clean numeric course code from a raw string.

    The scraper sometimes produces strings like "Course code:214213" instead
    of the bare code "214213".  Strip the prefix and any surrounding whitespace
    so the result can be looked up directly in the courses dict.
    """
    code = raw.strip()
    if code.lower().startswith("course code:"):
        code = code[len("course code:"):].strip()
    return code


def parse_offerings(raw: list | None) -> tuple[Offering, ...]:
    """
    Parse raw offering dicts from the dataset into Offering objects.

    Normalises semester strings to planner codes (S1, S2, SS).
    "Double Semester" and "Full Year" each produce two Offering objects
    (one for S1, one for S2) so the planner can schedule them in either
    semester independently.

    Campus codes are passed through as-is; unknown values fall back to "D".
    Delivery mode codes (DIS/INT/BLK) are normalised; unknowns fall back to "DIS".
    """
    offerings = []

    if not raw:
        return ()

    for o in raw:
        raw_semester = o.get("semester") or o.get("teachingPeriod")
        raw_campus = o.get("campus_code") or o.get("campus") or o.get("location") or ""
        raw_mode = o.get("delivery_mode") or o.get("deliveryMode") or o.get("mode") or ""

        if not raw_semester:
            continue

        campus = normalize_campus(str(raw_campus).strip())
        mode = normalize_mode(str(raw_mode))

        # Multi-semester entries expand into one Offering per semester code.
        if raw_semester in _MULTI_SEMESTER_MAP:
            for sem_code in _MULTI_SEMESTER_MAP[raw_semester]:
                offerings.append(Offering(semester=sem_code, campus=campus, mode=mode))
            continue

        sem_code = _SEMESTER_MAP.get(raw_semester)
        if not sem_code:
            # Unknown semester string: skip rather than store a value
            # the planner cannot match.
            continue

        offerings.append(Offering(semester=sem_code, campus=campus, mode=mode))

    return tuple(offerings)


def _parse_code_set(raw: list, own_code: str = "") -> frozenset:
    """
    Build a frozenset of clean course codes from a raw list.

    Used for corequisites and restrictions, where the value is a flat list of
    codes with no AND/OR structure.  Self-references are removed.
    Returns an empty frozenset when raw is falsy.
    """
    if not raw:
        return frozenset()
    return frozenset(
        clean
        for code in raw
        if (clean := sanitize_course_code(code)) and clean != own_code
    )


def _build_expr_from_struct(node: Any, own_code: str = "") -> PrerequisiteExpression | None:
    """
    Recursively convert a structured prerequisite tree (from the new scraper
    format) into a PrerequisiteExpression.

    Accepted node forms:
        None                 → None (no prerequisites)
        str  "115230"        → CoursePrerequisite("115230")
        dict {"op": "OR",  "args": [...]}  → OrExpression([...])
        dict {"op": "AND", "args": [...]}  → AndExpression([...])

    own_code is stripped to prevent self-referential prerequisites.
    Course codes not passing sanitize_course_code are skipped silently.
    """
    if node is None:
        return None

    if isinstance(node, str):
        code = sanitize_course_code(node)
        if not code or code == own_code or code in _ADMISSION_NOISE_CODES:
            return None
        return CoursePrerequisite(code)

    if isinstance(node, dict):
        op = node.get("op", "AND").upper()
        children = [
            _build_expr_from_struct(arg, own_code)
            for arg in node.get("args", [])
        ]
        children = [c for c in children if c is not None]
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        cls = AndExpression if op == "AND" else OrExpression
        return cls(tuple(children))

    return None


def parse_prereqs(
    prereqs: Any,
    own_code: str = "",
    own_level: int = 0,
    level_map: dict[str, int] = None,
) -> PrerequisiteExpression | None:
    """
    Build a PrerequisiteExpression from raw prerequisite data.

    Accepts two formats:

    **Old format** (flat list from the original regex scraper)::

        ["115230", "115233", "627739"]

    Applies the plausibility filter when own_level and level_map are provided,
    retaining only same-subject prerequisites that are at a lower level or
    lexicographically earlier (for same-level sequential courses). Without
    level information, falls back to admission-noise stripping only.

    **New structured format** (from the HTML-aware scraper)::

        None | "115230" | {"op": "OR"|"AND", "args": [...]}

    The structured format is already accurate (parsed from the prerequisites
    section only), so no plausibility filter is applied -- only self-references
    and admission noise codes are removed.
    """
    if not prereqs and prereqs != 0:
        return None

    if isinstance(prereqs, (str, dict)):
        return _build_expr_from_struct(prereqs, own_code)

    if isinstance(prereqs, list):
        if level_map is not None and own_level > 0:
            clean_codes = [
                sanitize_course_code(code)
                for code in prereqs
                if (
                    sanitize_course_code(code) != own_code
                    and sanitize_course_code(code) not in _ADMISSION_NOISE_CODES
                    and _is_plausible_prereq(
                        own_code,
                        sanitize_course_code(code),
                        own_level,
                        level_map.get(sanitize_course_code(code), 100),
                    )
                )
            ]
        else:
            # No level info: strip noise and self-refs only
            clean_codes = [
                sanitize_course_code(code)
                for code in prereqs
                if sanitize_course_code(code) != own_code
            ]
            clean_codes = [
                c for c in clean_codes
                if c and c not in _ADMISSION_NOISE_CODES
            ]

        clean_codes = [c for c in clean_codes if c]
        if not clean_codes:
            return None
        exprs: list[PrerequisiteExpression] = [
            CoursePrerequisite(code) for code in clean_codes
        ]
        if len(exprs) == 1:
            return exprs[0]
        return AndExpression(tuple(exprs))

    return None


def _is_plausible_prereq(
    own_code: str,
    prereq_code: str,
    own_level: int,
    prereq_level: int,
) -> bool:
    """
    Return True if prereq_code is a plausible academic prerequisite for own_code.

    The old regex scraper grabbed every six-digit code visible on a course page,
    not just codes in the prerequisites section. This produced two classes of noise:

    1. Admission gatekeepers (627739, 219206) -- stripped separately via
       _ADMISSION_NOISE_CODES.
    2. Codes from "related courses", navigation, or course family overviews that
       appear on the same page. These create spurious cross-subject cycles.

    A prerequisite is considered plausible when:
    - It is from the same subject area (same 3-digit Massey prefix) AND the
      prerequisite code is lexicographically earlier (for same-level sequential
      courses like 159101 -> 159102) or at a strictly lower level.

    Cross-subject prerequisites are always dropped from flat-list data: the old
    scraper cannot distinguish "COMP300 requires MATH200" from "MATH200 appeared
    in the COMP300 page header". The new HTML-aware scraper captures these
    correctly from the prerequisites section, so re-scraping will restore them.

    Args:
        own_code:     The course being loaded.
        prereq_code:  The candidate prerequisite code.
        own_level:    NZQF level band of own_code (100, 200, 300, ...).
        prereq_level: NZQF level band of prereq_code.
    """
    if prereq_code == own_code:
        return False
    same_subject = own_code[:3] == prereq_code[:3]
    if not same_subject:
        return False
    # Same subject: keep if strictly lower level, or same level but earlier code.
    if prereq_level < own_level:
        return True
    if prereq_level == own_level:
        return prereq_code < own_code
    return False


def _strip_phantom_prereqs(
    expr: PrerequisiteExpression | None,
    known_codes: frozenset[str],
) -> PrerequisiteExpression | None:
    """
    Remove prerequisite codes that do not exist in the catalogue.

    The old flat-list scraper captured every six-digit code visible on a
    course page, including codes for retired courses that no longer appear in
    the catalogue. These phantom codes are harmless at scheduling time (the
    out-of-scope bypass in prereqs_met handles them), but they pollute the
    prerequisite graph and trigger false warnings in the CLI audit.

    Returns the stripped expression, or None if all branches are removed.
    """
    if expr is None:
        return None
    if isinstance(expr, CoursePrerequisite):
        return expr if expr.code in known_codes else None
    if isinstance(expr, (AndExpression, OrExpression)):
        children = [
            c for child in expr.children
            if (c := _strip_phantom_prereqs(child, known_codes)) is not None
        ]
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return type(expr)(tuple(children))
    return expr


def load_courses() -> dict[str, Course]:
    """
    Load courses.json and return a dict mapping course_code -> Course.

    Three-pass approach:
    Pass 1 -- build the full set of known course codes.
    Pass 2 -- build a level map (code -> level) for the prerequisite plausibility filter.
    Pass 3 -- construct Course objects with filtered prerequisites.

    Prerequisite filtering removes:
    - Self-references (a course listed as its own prerequisite).
    - Admission gatekeeper codes (627739, 219206) that appear on every page.
    - Cross-subject codes and same-subject codes at a higher or equal level
      (scraping noise from the old flat-list regex approach).
    - Phantom codes: six-digit codes that don't correspond to any course in
      the catalogue (retired/deprecated courses picked up as scraping noise).
    """
    if not DATASET_PATH.exists():
        raise FileNotFoundError(
            "courses.json not found. Run ingestion/build_dataset.py first."
        )

    with open(DATASET_PATH, encoding="utf8") as f:
        raw_courses = json.load(f)

    # Quick schema sanity check: must be a list of dicts with required keys.
    _validate_courses_schema(raw_courses)

    # Pass 1: build the known-code set for phantom stripping.
    known_codes: frozenset[str] = frozenset(
        item["course_code"] for item in raw_courses if item.get("course_code")
    )

    # Pass 2: build level lookup
    level_map: dict[str, int] = {}
    for item in raw_courses:
        code = item.get("course_code")
        if code:
            level_map[code] = int(item.get("level") or item.get("course_level") or 100)

    # Pass 3: construct Course objects
    courses = {}

    for item in raw_courses:
        code = item.get("course_code")
        if not code:
            continue

        offerings = parse_offerings(item.get("offerings"))
        own_level = level_map.get(code, 100)

        # After repair_dataset.py runs, prerequisites are already cleaned:
        # self-refs, noise, and phantoms removed. Pass level_map=None to skip
        # the aggressive plausibility filter that strips cross-subject prereqs.
        # This preserves legitimate cross-subject prerequisites (e.g. CS courses
        # requiring Maths) that were previously stripped by the old heuristic.
        prereq_expr = parse_prereqs(
            item.get("prerequisites"),
            own_code=code,
            own_level=own_level,
            level_map=None,  # skip plausibility filter - data is pre-cleaned
        )
        # Still strip any phantom codes that somehow survived repair.
        prereq_expr = _strip_phantom_prereqs(prereq_expr, known_codes)

        try:
            raw_credits = item.get("credits")
            # Explicitly handle anomalous credit values.
            # Zero-credit courses (practicums, language courses) are non-schedulable
            # and should not be silently upgraded to 15cr.
            # Very large values (90, 120, 240) are research/thesis courses and
            # should be stored as-is so the planner can handle them explicitly.
            if raw_credits is None or raw_credits == "":
                credits = 15  # sensible default for missing data
            else:
                credits = int(float(raw_credits))
                if credits <= 0:
                    # Keep as 0 - the scheduler will skip zero-credit courses
                    credits = 0

            course = Course(
                code=code,
                title=item.get("title", ""),
                credits=credits,
                level=own_level,
                offerings=offerings,
                prerequisites=prereq_expr,
                corequisites=_parse_code_set(item.get("corequisites", []), own_code=code),
                restrictions=_parse_code_set(item.get("restrictions", []), own_code=code),
                url=item.get("url") or None,
                description=item.get("intro") or item.get("description") or None,
            )
        except Exception as exc:
            logger.warning(
                "Skipping course %r: failed to construct Course object: %s",
                code, exc,
            )
            continue

        courses[code] = course

    logger.info("Loaded %d courses from dataset", len(courses))
    return courses


def _build_major_requirement_dict(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a single raw majors.json entry into a serialized RequirementNode dict.

    The dataset stores majors in one of two pool formats:

    *Old format* (bare list of code strings, credits unknown)::

        {"url": "...", "required": [...], "elective_pools": [["Course code:214213", ...]]}

    *Backfilled format* (dict with credits extracted from the Massey page)::

        {"url": "...", "required": [...], "elective_pools": [
            {"credits": 45, "courses": ["Course code:214213", ...]}
        ]}

    Both formats are supported.  Old-format pools use ``credits=0`` as before.
    Backfilled pools use the stored credit value.
    """
    children: list[dict[str, Any]] = []

    for code in entry.get("required", []):
        clean = sanitize_course_code(code)
        if clean:
            children.append({"type": "COURSE", "course_code": clean})

    for pool in entry.get("elective_pools", []):
        # New backfilled format: {"credits": N, "courses": [...]}
        if isinstance(pool, dict):
            credits = int(pool.get("credits") or 0)
            raw_codes = pool.get("courses", [])
        else:
            # Old format: bare list of "Course code:XXXXXX" strings
            credits = 0
            raw_codes = pool

        codes = [sanitize_course_code(c) for c in raw_codes if isinstance(c, str)]
        codes = [c for c in codes if c]
        if codes:
            children.append({
                "type": "CHOOSE_CREDITS",
                "credits": credits,
                "course_codes": codes,
            })

    return {"type": "ALL_OF", "children": children}


def load_majors() -> list[dict[str, Any]]:
    """
    Load majors.json and return a normalised list of major dicts.

    Accepts two formats:

    * **List format** (target format): each element is already
      ``{"name": str, "url": str, "requirement": <node dict>}``.
      Returned as-is.

    * **Dict format** (current scraper output): the file is a dict keyed by
      major name where each value is ``{"url": str, "required": [...],
      "elective_pools": [...]}``.  Converted to the list format on the fly by
      building an ALL_OF requirement tree from the raw lists.

    Callers always receive the list format regardless of which is on disk.
    """
    if not MAJORS_DATASET_PATH.exists():
        raise FileNotFoundError(
            "datasets/majors.json not found. Run ingestion/build_majors_dataset.py first."
        )
    with open(MAJORS_DATASET_PATH, encoding="utf-8") as f:
        raw = json.load(f)

    # Quick schema sanity check before processing.
    _validate_majors_schema(raw)

    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        return [
            {
                "name": name,
                "url": entry.get("url", ""),
                "requirement": _build_major_requirement_dict(entry),
            }
            for name, entry in raw.items()
            if isinstance(entry, dict)
        ]

    raise ValueError(
        f"datasets/majors.json has unexpected top-level type {type(raw).__name__}. "
        "Expected a list or dict."
    )