"""
patch_elective_gaps.py - Post-scrape patch to fix majors with insufficient
credit coverage in their requirement trees.

Every NZ bachelor degree has a free-elective component (students choose courses
to make up the full credit total). The Massey scraper captures major-specific
requirements but not the general free-elective quota, which is documented on
the degree overview page rather than individual major pages.

This script:
1. Calculates the credit gap for each major (degree_target - tree_credits)
2. For majors with a gap > 15cr, adds a CHOOSE_CREDITS free-elective node
3. The elective pool is all schedulable courses at the appropriate level(s)
   that are not already in the major's requirement tree

Run after build_majors_dataset.py and repair_dataset.py:
    python -m coursemap.ingestion.patch_elective_gaps
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"


def _total_req_credits(req: dict, courses: dict) -> int:
    t = req.get("type", "")
    if t == "COURSE":
        c = courses.get(req.get("course_code", ""))
        return c.get("credits", 0) if c else 0
    elif t == "CHOOSE_CREDITS":
        return req.get("credits", 0)
    elif t == "ALL_OF":
        return sum(_total_req_credits(ch, courses) for ch in req.get("children", []))
    return 0


def _collect_codes(req: dict, result: set | None = None) -> set:
    if result is None:
        result = set()
    t = req.get("type", "")
    if t == "COURSE":
        result.add(req.get("course_code", ""))
    elif t == "CHOOSE_CREDITS":
        result.update(req.get("course_codes", []))
    elif t == "ALL_OF":
        for ch in req.get("children", []):
            _collect_codes(ch, result)
    return result


def _has_free_elective_node(req: dict) -> bool:
    """Return True if the requirement tree already has a free-elective node."""
    if req.get("type") == "CHOOSE_CREDITS" and req.get("label", "").startswith("Free elective"):
        return True
    for ch in req.get("children", []):
        if _has_free_elective_node(ch):
            return True
    return False


def patch(courses_path: Path | None = None, majors_path: Path | None = None,
          specs_path: Path | None = None, quals_path: Path | None = None) -> int:
    """
    Patch majors.json in-place.  Returns the number of majors patched.
    """
    courses_path = courses_path or _DATASETS_DIR / "courses.json"
    majors_path  = majors_path  or _DATASETS_DIR / "majors.json"
    specs_path   = specs_path   or _DATASETS_DIR / "specialisations.json"
    quals_path   = quals_path   or _DATASETS_DIR / "qualifications.json"

    courses_list = json.loads(courses_path.read_text(encoding="utf-8"))
    majors       = json.loads(majors_path.read_text(encoding="utf-8"))
    specs        = json.loads(specs_path.read_text(encoding="utf-8"))
    quals        = json.loads(quals_path.read_text(encoding="utf-8"))

    courses   = {c["course_code"]: c for c in courses_list}
    spec_map  = {s["title"]: s for s in specs}
    qual_map  = {q["qual_code"]: q for q in quals}

    patched = 0
    for m in majors:
        spec = spec_map.get(m["name"])
        if not spec:
            continue

        level  = spec.get("level", 7)
        length = spec.get("length", 3)
        target = length * 120

        tree_credits = _total_req_credits(m["requirement"], courses)
        gap = target - tree_credits

        if gap <= 15:
            continue  # complete or over-specified

        if _has_free_elective_node(m["requirement"]):
            continue  # already patched

        existing_codes = _collect_codes(m["requirement"])
        # NOTE: course_codes is intentionally empty here.
        # The ElectiveFiller in generate_filled_plan() handles intelligent selection
        # of same-subject electives when auto_fill=True. Populating course_codes with
        # the full catalogue would cause the base-plan scheduler to pick courses 
        # alphabetically (e.g. Accounting before Computing), bypassing the filler.
        free_elective_node = {
            "type": "CHOOSE_CREDITS",
            "credits": gap,
            "course_codes": [],
            "label": f"Free electives ({gap}cr) - choose from any Massey courses",
        }

        if m["requirement"]["type"] == "ALL_OF":
            m["requirement"]["children"].append(free_elective_node)
        else:
            m["requirement"] = {
                "type": "ALL_OF",
                "children": [m["requirement"], free_elective_node],
            }

        patched += 1
        logger.debug("Patched %s: +%dcr free electives", m["name"], gap)

    if patched:
        majors_path.write_text(
            json.dumps(majors, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("Patched %d major(s); saved %s", patched, majors_path)
    else:
        logger.info("No majors needed patching.")

    return patched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = patch()
    print(f"Patched {n} major(s).")
