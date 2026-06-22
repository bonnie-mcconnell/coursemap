"""
Tree traversal utilities for requirement nodes.
Work on arbitrarily nested requirement trees. Used for enumeration (e.g. solver), not validation.
"""
from __future__ import annotations

from .requirement_nodes import (
    AllOfRequirement,
    AnyOfRequirement,
    ChooseCreditsRequirement,
    ChooseNRequirement,
    CourseRequirement,
    MajorRequirement,
    MinLevelCreditsFromRequirement,
    RequirementNode,
    TotalCreditsRequirement,
)


def collect_course_codes(node: RequirementNode) -> set[str]:
    """Recursively collect all course codes mentioned anywhere in the tree."""
    out: set[str] = set()
    if isinstance(node, CourseRequirement):
        out.add(node.course_code)
    elif isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            out |= collect_course_codes(c)
    elif isinstance(
        node,
        (ChooseCreditsRequirement, ChooseNRequirement, MinLevelCreditsFromRequirement),
    ):
        out.update(node.course_codes)
    elif isinstance(node, MajorRequirement):
        out |= collect_course_codes(node.requirement)
    return out


def collect_elective_nodes(
    node: RequirementNode,
) -> list[ChooseCreditsRequirement | ChooseNRequirement]:
    """Recursively collect all CHOOSE_CREDITS and CHOOSE_N nodes at any depth."""
    out: list[ChooseCreditsRequirement | ChooseNRequirement] = []
    if isinstance(node, (ChooseCreditsRequirement, ChooseNRequirement)):
        out.append(node)
    if isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            out.extend(collect_elective_nodes(c))
    elif isinstance(node, MajorRequirement):
        out.extend(collect_elective_nodes(node.requirement))
    return out


def collect_degree_elective_nodes(
    node: RequirementNode,
    *,
    under_major: bool = False,
) -> list[ChooseCreditsRequirement | ChooseNRequirement]:
    """
    Collect CHOOSE_CREDITS and CHOOSE_N nodes that are NOT inside any MAJOR subtree.

    Used by the solver to find degree-level elective pools: pools that are
    requirements of the degree itself rather than of a particular major.
    Major-level pools are handled separately via collect_elective_nodes on each
    major's own requirement tree.

    This mirrors collect_core_course_codes: recursion stops at MajorRequirement
    boundaries so that the two sources of pools are never double-counted.
    """
    out: list[ChooseCreditsRequirement | ChooseNRequirement] = []
    if isinstance(node, (ChooseCreditsRequirement, ChooseNRequirement)):
        if not under_major:
            out.append(node)
    if isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            out.extend(collect_degree_elective_nodes(c, under_major=under_major))
    elif isinstance(node, MajorRequirement):
        # Stop here. Major-level pools are collected separately via
        # collect_elective_nodes(major_req) to avoid double-counting.
        return out
    return out


def collect_course_node_codes(node: RequirementNode) -> set[str]:
    """
    Collect codes from COURSE requirement nodes only, ignoring pool members.

    Unlike collect_course_codes, this does not recurse into ChooseCreditsRequirement
    nodes. Used to find codes that are individually required (COURSE nodes) even
    when those same codes also appear in elective pools.
    """
    if isinstance(node, CourseRequirement):
        return {node.course_code}
    if isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        result: set[str] = set()
        for child in node.children:
            result |= collect_course_node_codes(child)
        return result
    if isinstance(node, MajorRequirement):
        return collect_course_node_codes(node.requirement)
    return set()


def collect_major_nodes(node: RequirementNode) -> list[MajorRequirement]:
    """Recursively collect all MAJOR nodes at any depth."""
    out: list[MajorRequirement] = []
    if isinstance(node, MajorRequirement):
        out.append(node)
        out.extend(collect_major_nodes(node.requirement))
    elif isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            out.extend(collect_major_nodes(c))
    return out


def find_total_credits(node: RequirementNode) -> int:
    """Return required_credits from the first TOTAL_CREDITS node found (pre-order). 0 if none."""
    if isinstance(node, TotalCreditsRequirement):
        return node.required_credits
    if isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            n = find_total_credits(c)
            if n != 0:
                return n
    elif isinstance(node, MajorRequirement):
        return find_total_credits(node.requirement)
    return 0


def collect_core_course_codes(node: RequirementNode, *, under_major: bool = False) -> set[str]:
    """
    Collect course codes from COURSE nodes that are not inside a major.
    Used for degree-level required (core) courses; does not include codes from CHOOSE_* nodes.
    """
    out: set[str] = set()
    if isinstance(node, CourseRequirement):
        if not under_major:
            out.add(node.course_code)
    elif isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        for c in node.children:
            out |= collect_core_course_codes(c, under_major=under_major)
    elif isinstance(node, MajorRequirement):
        out |= collect_core_course_codes(node.requirement, under_major=True)
    return out


def select_free_electives(
    courses: dict,
    planned_codes: set,
    excluded_codes: set,
    gap: int,
    campus: str,
    mode: str,
    preferred: frozenset | None = None,
    max_level_override: int | None = None,
) -> list[tuple[int, str, str]]:
    """
    Select subject-area elective courses to fill a free-elective credit gap.

    Returns a list of (level, code, title) tuples ordered by relevance:
    dominant-prefix courses first, then secondary subjects, then by level
    and code. Suitable both for display (CLI suggestions) and for use as
    filler in auto-fill planning.

    Args:
        courses:            Full course catalogue (code -> Course).
        planned_codes:      Codes already in the plan (excluded from selection).
        excluded_codes:     Additional codes to exclude (--exclude flag + prior).
        gap:                Free-elective credit gap to fill.
        campus:             Campus filter (e.g. 'D').
        mode:               Delivery mode filter (e.g. 'DIS').
        preferred:          Course codes to prioritise (--prefer flag).
        max_level_override: Override the auto-detected maximum level cap.
    """
    from collections import Counter

    if gap <= 0:
        return []

    if preferred is None:
        preferred = frozenset()

    all_excluded = set(planned_codes) | set(excluded_codes)
    prefix_counts = Counter(code[:3] for code in planned_codes)
    prefix_rank   = {pfx: rank for rank, (pfx, _) in enumerate(prefix_counts.most_common())}

    max_planned_level = max_level_override or max(
        (courses[c].level for c in planned_codes if c in courses),
        default=100,
    )

    candidates: list[tuple] = []
    seen: set = set()

    for code, course in courses.items():
        if code in all_excluded or code in seen:
            continue
        if course.credits <= 0:
            continue  # skip zero-credit practicums
        if not any(o.campus == campus and o.mode == mode for o in course.offerings):
            continue
        if course.level > 300 or course.level > max_planned_level + 100:
            continue
        pfx = code[:3]
        if pfx in prefix_rank:
            prefer_flag = 0 if code in preferred else 1
            candidates.append((prefer_flag, prefix_rank[pfx], course.level, code, course.title))
            seen.add(code)

    candidates.sort()

    # Two-pass selection:
    # Pass 1 - preferred and prefix-matching courses in sort order (best fit first).
    # Pass 2 - if still under gap, add remaining courses sorted by credits ascending
    #           (smallest first) so we can pack the gap precisely.
    selected: list[tuple[int, str, str]] = []
    selected_codes: set[str] = set()
    running = 0

    for _, _, level, code, title in candidates:
        if running >= gap:
            break
        cr = courses[code].credits
        # Skip courses that would push us too far over the gap.
        # Allow a single course to exceed the gap by at most one course worth
        # of credits (15cr typically) - this prevents the selector getting stuck
        # just under the gap when only larger courses are left.
        if running + cr > gap + 30:
            continue
        selected.append((level, code, title))
        selected_codes.add(code)
        running += cr

    return selected
