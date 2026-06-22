"""
Degree requirement tree derivation from Massey's qualification and major data.

Instead of a hardcoded degree_requirements.json that covers four majors, this
module derives the complete requirement tree for any (qualification, major) pair
using:

  - qualifications.json  -- level (4-10) and length (1-5 years) per qualification
  - specialisations.json -- maps each major title to a qual_code
  - majors.json          -- required course lists and elective pools per major

Massey uses a standard credit structure: 120 credits per full-time year.
Level constraints are drawn from the public Massey undergraduate handbook.

The output is a RequirementNode tree identical in structure to what
degree_requirements.json used to provide, but covering every major in the
dataset rather than four hand-picked ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coursemap.domain.requirement_nodes import (
    AllOfRequirement,
    AnyOfRequirement,
    ChooseCreditsRequirement,
    CourseRequirement,
    RequirementNode,
    TotalCreditsRequirement,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Massey credit and level rules by qualification level and length
# ---------------------------------------------------------------------------
#
# Source: Massey University undergraduate and postgraduate handbooks (public).
# Rule encoding: (nzqf_level, length_years) -> DegreeProfile
#
# Credits per year: 120 (standard full-time load at Massey).
# Level constraints apply only to undergraduate (Level 7) bachelor degrees;
# postgraduate qualifications have different and more variable structures.

@dataclass(frozen=True)
class DegreeProfile:
    """Credit and level constraints for one category of Massey qualification."""
    total_credits: int
    # Maximum credits from Level 100 courses (prevents first-year overload).
    max_level_100: int | None = None
    # Minimum credits from Level 300 courses (ensures third-year specialisation).
    min_level_300: int | None = None
    # Minimum credits from Level 400 courses (honours-level depth).
    min_level_400: int | None = None


# Keyed by (nzqf_level, length_years). Missing combinations fall back to
# total_credits = length * 120 with no level constraints.
_DEGREE_PROFILES: dict[tuple, DegreeProfile] = {
    # Standard 3-year bachelor's (BHSc, BSc, BA, BBus, etc.)
    (7, 3): DegreeProfile(
        total_credits=360,
        max_level_100=165,
        min_level_300=75,
    ),
    # 4-year bachelor's (BSW, BEng, some professional degrees)
    (7, 4): DegreeProfile(
        total_credits=480,
        max_level_100=165,
        min_level_300=135,
    ),
    # 5-year bachelor's (BVSc)
    (7, 5): DegreeProfile(
        total_credits=600,
        max_level_100=165,
        min_level_300=135,
    ),
    # Graduate diploma / graduate certificate (1yr, Level 7)
    (7, 1): DegreeProfile(total_credits=120),
    # 2-year graduate diploma (rare)
    (7, 2): DegreeProfile(total_credits=240),
    # Postgraduate diploma and certificate (1yr, Level 8).
    # Honours degrees are also Level 8, 1yr at Massey, but their courses are
    # at L700+ rather than L400, so no minimum level constraint is enforced --
    # the postgraduate level requirement is implicit in the course catalogue.
    (8, 1): DegreeProfile(total_credits=120),
    # 4-year bachelor with honours integrated (BFA Hons, BDes Hons)
    (8, 4): DegreeProfile(
        total_credits=480,
        max_level_100=165,
    ),
    # Taught master's (2yr, Level 9)
    (9, 2): DegreeProfile(total_credits=240),
    # Accelerated master's (1yr, Level 9)
    (9, 1): DegreeProfile(total_credits=120),
}


def profile_for(level: int, length: int) -> DegreeProfile:
    """Return the DegreeProfile for a qualification, with a safe fallback."""
    profile = _DEGREE_PROFILES.get((level, length))
    if profile is not None:
        return profile
    # Fallback: 120cr per year, no level constraints. Covers unusual combinations
    # (Level 4-6 foundation/diploma, Level 10 doctorates, etc.).
    return DegreeProfile(total_credits=length * 120)



def cap_overcaptured_required_codes(
    required_codes: set[str],
    pool_nodes: list,
    degree_total: int,
    course_credits: dict[str, int],
    course_levels: dict[str, int],
    schedulable_codes: frozenset,
    tree_required: set[str],
) -> tuple[set[str], bool]:
    """
    Decide which required-course codes to keep when a major's scraped data
    "over-captures" more required courses than the degree's credit target
    allows for (e.g. a 120cr diploma whose major data lists 165cr of
    `CourseRequirement` codes - almost always because the source data
    flattened several alternative specialisation tracks into one list,
    rather than the degree genuinely requiring more credits than it awards).

    Returns (kept_codes, was_capped). When was_capped is False, kept_codes
    equals required_codes unchanged.

    This mirrors the cap decision PlanSearch._plan_for_major makes during
    generation (same sort priority: in-tree > not, schedulable > not, lower
    level > higher, then code for determinism) so that a standalone caller
    with no visibility into a specific generated plan - like the
    /api/plan/validate endpoint, which rebuilds the requirement tree fresh
    rather than reusing whatever trim a particular generation run applied -
    can still present a requirement tree that's actually satisfiable, rather
    than one that always reports "missing required course" for whichever
    courses happen to be cut by sort order, regardless of what the student
    actually completed.

    Note: this can only mirror the GENERIC cap decision (which courses any
    plan for this major would be expected to need), not the exact trim a
    SPECIFIC already-generated plan happened to apply (e.g. if that plan's
    elective pool selections differed from the generic default). For majors
    with this over-capture pattern, validation is therefore necessarily a
    best-effort approximation - the root issue is the source data conflating
    several alternative requirement sets into one, which no validation-side
    workaround can fully resolve.
    """
    if degree_total <= 0:
        return required_codes, False

    req_total = sum(course_credits.get(c, 0) for c in required_codes)
    schedulable_pool_contribution = sum(
        min(
            n.credits,
            sum(
                course_credits.get(c, 0) for c in n.course_codes
                if c in schedulable_codes
            ),
        )
        for n in pool_nodes
    )

    req_budget = (
        max(0, degree_total - schedulable_pool_contribution)
        if req_total + schedulable_pool_contribution > degree_total
        else degree_total
    )
    if req_total <= req_budget:
        return required_codes, False

    def _req_sort(code: str) -> tuple:
        credits = course_credits.get(code)
        if credits is None:
            return (1, 2, 999, code)
        in_tree = 0 if code in tree_required else 1
        has_matching = 0 if code in schedulable_codes else 1
        level = course_levels.get(code, 999)
        return (in_tree, has_matching, level, code)

    kept: set[str] = set()
    running = 0
    for code in sorted(required_codes, key=_req_sort):
        if running >= req_budget:
            break
        credits = course_credits.get(code)
        if credits is None:
            continue
        kept.add(code)
        running += credits
    return kept, True


def build_degree_tree(
    major_req: RequirementNode,
    qual_level: int,
    qual_length: int,
    major_name: str,
    schedulable_major_credits: int = 0,
    force_total_credits: bool = False,
    override_total_credits: int | None = None,
) -> RequirementNode:
    """
    Wrap a major's requirement tree with degree-level credit and level constraints.

    The result is a complete degree requirement tree::

        ALL_OF(
            [TOTAL_CREDITS(N)],              # when data complete OR force_total_credits=True
            <major_req>,
        )

    The TotalCreditsRequirement is included when the major dataset covers the
    full degree (schedulable_major_credits >= degree_credits) OR when
    force_total_credits=True (used by generate_filled_plan after augmenting the
    requirement tree with filler courses to reach the degree total).

    Args:
        major_req:                 Already-built RequirementNode for the major.
        qual_level:                NZQF level (7 = bachelor, 8 = honours, etc.).
        qual_length:               Duration in years.
        major_name:                Human-readable name (for log messages only).
        schedulable_major_credits: Total credits of major courses that have
                                   offerings. Used to decide whether to include
                                   the TotalCreditsRequirement automatically.
        force_total_credits:       If True, always include TotalCreditsRequirement
                                   regardless of schedulable_major_credits. Used
                                   by the filled-plan path after injecting filler
                                   courses that bring the working set to degree total.
    """
    profile = profile_for(qual_level, qual_length)
    children: list[RequirementNode] = []

    # Include TotalCreditsRequirement when:
    #   a) The scraped major data fully covers the degree (data_is_complete), or
    #   b) The caller explicitly requests it (force_total_credits=True), which the
    #      filled-plan path uses after augmenting the tree with filler courses.
    #
    # When neither condition holds the credit target is reported separately as a
    # "free elective gap" so the student knows they must self-select extra courses.
    data_is_complete = schedulable_major_credits >= profile.total_credits
    if data_is_complete or force_total_credits:
        _total = override_total_credits if override_total_credits is not None else profile.total_credits
        children.append(TotalCreditsRequirement(_total))

    children.append(major_req)
    return AllOfRequirement(tuple(children))


def filter_requirement_tree(
    node: RequirementNode,
    schedulable_codes: frozenset,
    course_credits: dict[str, int] | None = None,
    keep_open_pools: bool = False,
) -> RequirementNode | None:
    """
    Return a copy of node with unschedulable courses removed and pool credit
    targets adjusted to what the configured delivery mode can actually provide.

    Used to align the validation tree with the working set. Two adjustments:

    1. CourseRequirement nodes for courses with no matching offering are dropped.
    2. ChooseCreditsRequirement pool targets are capped at the total credits
       available from schedulable pool members. When a pool's DIS courses cannot
       meet the original credit target (e.g. field-work-heavy ecology courses
       that are internal-only), requiring the full amount would permanently fail
       distance plans. The cap aligns validation with what the scheduler achieves.

    Composite nodes with no remaining children are dropped entirely.
    Returns None if the entire subtree becomes empty after filtering.

    `keep_open_pools`: when True, an open free-elective pool (`open_pool=True`
    with no fixed course_codes) is preserved instead of dropped. Generation
    callers validate an UNFILLED base plan (free electives haven't been added
    yet) and should keep dropping it - pass False (the default). Callers that
    validate a COMPLETE plan, where free electives should genuinely be
    checked against what the student actually took, should pass True.
    """
    if isinstance(node, CourseRequirement):
        return node if node.course_code in schedulable_codes else None

    if isinstance(node, ChooseCreditsRequirement):
        if node.open_pool and not node.course_codes:
            return node if keep_open_pools else None
        schedulable_pool = tuple(c for c in node.course_codes if c in schedulable_codes)
        if not schedulable_pool:
            return None
        if course_credits is not None:
            available_cr = sum(course_credits.get(c, 0) for c in schedulable_pool)
            effective_credits = min(node.credits, available_cr)
        else:
            effective_credits = node.credits
        if effective_credits == node.credits and schedulable_pool == node.course_codes:
            return node
        return ChooseCreditsRequirement(
            credits=effective_credits,
            course_codes=schedulable_pool,
        )

    if isinstance(node, AllOfRequirement):
        children = [
            filtered
            for child in node.children
            if (filtered := filter_requirement_tree(
                child, schedulable_codes, course_credits, keep_open_pools
            )) is not None
        ]
        if not children:
            return None
        return AllOfRequirement(tuple(children))

    if isinstance(node, AnyOfRequirement):
        children = [
            filtered
            for child in node.children
            if (filtered := filter_requirement_tree(
                child, schedulable_codes, course_credits, keep_open_pools
            )) is not None
        ]
        if not children:
            return None
        return AnyOfRequirement(tuple(children))

    # TotalCreditsRequirement, MinLevelCreditsRequirement, MaxLevelCreditsRequirement, etc.
    return node
