"""
Orchestration layer between the CLI and the planning/search engine.

PlannerService is the single entry point for plan generation. It:

  1. Resolves the user's major name to the matching dataset entry.
  2. Derives the degree requirement tree from qualification metadata
     (replacing the old hardcoded degree_requirements.json approach).
  3. Constructs the PlanGenerator template and PlanSearch instance.
  4. Returns the best valid DegreePlan along with diagnostics.

The service intentionally knows nothing about scheduling logic; that lives
in planner.generator. It knows nothing about the CLI; that lives in cli.main.
"""

from __future__ import annotations

import difflib
import json
import logging
from pathlib import Path

from coursemap.domain.course import Course
from coursemap.domain.plan import DegreePlan
from coursemap.domain.requirement_nodes import (
    AllOfRequirement,
    ChooseCreditsRequirement,
    RequirementNode,
    TotalCreditsRequirement,
)
from coursemap.domain.requirement_serialization import requirement_from_dict
from coursemap.domain.requirement_utils import (
    collect_course_codes,
    collect_elective_nodes,
    find_total_credits,
)
from coursemap.optimisation.search import PlanSearch
from coursemap.planner.generator import PlanGenerator
from coursemap.planner.elective_filler import ElectiveFiller
from coursemap.rules.degree_rules import build_degree_tree, filter_requirement_tree, profile_for

logger = logging.getLogger(__name__)

_DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"


def _load_qualification_map() -> dict[str, dict]:
    """
    Return a dict mapping major title -> qualification metadata.

    Built by joining specialisations.json (major title -> qual_code) with
    qualifications.json (qual_code -> level, length, title).
    """
    specs_path = _DATASETS_DIR / "specialisations.json"
    quals_path = _DATASETS_DIR / "qualifications.json"

    if not specs_path.exists() or not quals_path.exists():
        logger.warning(
            "specialisations.json or qualifications.json not found; "
            "degree rules will fall back to 360cr/no-level-constraints for all majors."
        )
        return {}

    specs = json.loads(specs_path.read_text(encoding="utf-8"))
    quals = json.loads(quals_path.read_text(encoding="utf-8"))

    qual_by_code = {q["qual_code"]: q for q in quals}
    return {
        s["title"]: qual_by_code[s["qual_code"]]
        for s in specs
        if s["qual_code"] in qual_by_code
    }


class PlannerService:
    """
    Orchestrates degree plan generation for a given major and student state.

    Attributes:
        courses:            Full course catalogue, code -> Course.
        majors:             Raw major list from majors.json (after load_majors()).
        _qual_map:          Maps major title to qualification metadata.
    """

    def __init__(
        self,
        courses: dict[str, Course],
        majors: list[dict],
    ):
        self.courses = courses
        self.majors = majors
        self._qual_map = _load_qualification_map()
        self.last_plan_stats = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_best_plan(
        self,
        major_name: str | None = None,
        max_credits_per_semester: int = 60,
        max_courses_per_semester: int | None = None,
        campus: str = "D",
        mode: str = "DIS",
        start_year: int = 2026,
        start_semester: str = "S1",
        prior_completed: frozenset = frozenset(),
        preferred_electives: frozenset = frozenset(),
        excluded_courses: frozenset = frozenset(),
        no_summer: bool = False,
        transfer_credits: int = 0,
    ) -> DegreePlan:
        """
        Generate the best valid degree plan for the given major.

        Args:
            major_name:               Partial or full major name. None plans all majors.
            max_credits_per_semester: Credit cap per semester (default 60, full-time).
            max_courses_per_semester: Course count cap per semester (None = no limit).
            campus:                   Campus code filter (e.g. 'D' for distance).
            mode:                     Delivery mode filter (e.g. 'DIS', 'INT').
            start_year:               First calendar year of study.
            prior_completed:          Course codes already completed before this plan.
            preferred_electives:      Course codes to prefer when selecting electives.
            excluded_courses:         Course codes to never schedule (student opt-out).

        Returns:
            The highest-scoring valid DegreePlan found.

        Raises:
            ValueError: No matching major, ambiguous name, or no valid plan exists.
        """
        resolved = self._resolve_major(major_name)

        parsed_majors = []
        for m in resolved:
            req_tree = self._build_major_req_tree(m)
            degree_tree = self._build_degree_tree(m, req_tree, campus=campus, mode=mode)
            parsed_majors.append({
                "name": m["name"],
                "url": m.get("url", ""),
                "raw": m,
                "requirement": req_tree,
                "degree_tree": degree_tree,
            })

        generator_template = PlanGenerator(
            self.courses,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            start_semester=start_semester,
            no_summer=no_summer,
        )

        search = PlanSearch(
            courses=self.courses,
            majors=parsed_majors,
            generator_template=generator_template,
            prior_completed=prior_completed,
            preferred_electives=preferred_electives,
            excluded_courses=excluded_courses,
        )

        plan = search.search()
        self.last_plan_stats = search.best_generator_stats
        if transfer_credits > 0:
            plan.transfer_credits = transfer_credits
        return plan

    def resolve_major(self, major_name: str | None) -> list[dict]:
        """
        Public name resolution. Returns the list of matching raw major dicts.

        Accepts exact or case-insensitive substring names. Raises ValueError
        on ambiguous or missing matches so callers can provide a useful error.
        """
        return self._resolve_major(major_name)

    def degree_tree_for_major(
        self,
        major_name: str,
        campus: str = "D",
        mode: str = "DIS",
    ) -> RequirementNode | None:
        """
        Return the full degree requirement tree for a named major.

        Returns None when the name matches zero or more than one major so the
        caller can skip validation gracefully without raising.
        """
        try:
            resolved = self._resolve_major(major_name)
        except ValueError:
            return None
        if len(resolved) != 1:
            return None
        m = resolved[0]
        return self._build_degree_tree(
            m,
            self._build_major_req_tree(m),
            campus=campus,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_major(self, major_name: str | None) -> list[dict]:
        if not major_name:
            return self.majors

        query = major_name.strip().lower()

        # Exact match (case-insensitive)
        exact = [m for m in self.majors if m["name"].lower() == query]
        if exact:
            return exact

        # Whole-string substring (handles "Computer Science – Bachelor of Science")
        partial = [m for m in self.majors if query in m["name"].lower()]
        if len(partial) == 1:
            return partial
        if len(partial) > 1:
            # Apply smart disambiguation before raising: prefer undergrad over postgrad,
            # then prefer BSc/BA over named professional degrees.
            smart = self._smart_disambiguate(partial, query)
            if smart is not None:
                return smart
            names = "\n  ".join(m["name"] for m in partial)
            raise ValueError(
                f"'{major_name}' matches {len(partial)} majors -- be more specific:\n  {names}"
            )

        # Word-overlap: every query word must appear as a substring of some
        # token in the major name. Handles abbreviations like 'comp sci',
        # 'maths', 'psych' that don't substring-match the full name.
        words = query.split()
        def word_match(name: str) -> bool:
            tokens = name.lower().replace("–", " ").split()
            return all(any(w in tok for tok in tokens) for w in words)

        word_matches = [m for m in self.majors if word_match(m["name"])]
        if len(word_matches) == 1:
            return word_matches
        if len(word_matches) > 1:
            smart = self._smart_disambiguate(word_matches, query)
            if smart is not None:
                return smart
            names = "\n  ".join(m["name"] for m in word_matches)
            raise ValueError(
                f"'{major_name}' matches {len(word_matches)} majors -- be more specific:\n  {names}"
            )

        # Last resort: difflib sequence similarity for typos / transpositions.
        all_names = [m["name"] for m in self.majors]
        suggestions = difflib.get_close_matches(major_name, all_names, n=5, cutoff=0.35)
        # If difflib finds nothing, try word-overlap scoring for partial suggestions.
        if not suggestions:
            query_words = set(query.split())
            scored = [
                (sum(1 for w in query_words if w in m["name"].lower()), m["name"])
                for m in self.majors
            ]
            suggestions = [name for sc, name in sorted(scored, reverse=True) if sc > 0][:5]
        hint = (
            "\n  " + "\n  ".join(suggestions) if suggestions
            else "\n  Run 'coursemap majors --search <keyword>' to browse."
        )
        raise ValueError(f"No major matching '{major_name}'. Did you mean:{hint}")

    def _smart_disambiguate(self, candidates: list[dict], query: str) -> list[dict] | None:
        """
        Given a list of ambiguous major matches, attempt to return a single best match.

        Strategy:
        1. If the query has no degree-type qualifier, strip postgrad options first.
        2. If one undergrad remains, return it.
        3. If multiple undergrads remain, prefer by standard degree order
           (BSc > BA > BBus > BHSc > BIS > other bachelor's).
        4. Return None if still ambiguous (caller will raise with full list).
        """
        _POSTGRAD_MARKERS = ("master", "honours", "postgrad", "graduate", "doctor", "phd",
                             "diploma", "certificate")
        _UNDERGRAD_MARKERS = ("bachelor",)
        query_has_qual = any(m in query for m in _POSTGRAD_MARKERS + _UNDERGRAD_MARKERS)

        if query_has_qual:
            return None  # User specified a qualifier - don't second-guess them

        undergrad = [
            m for m in candidates
            if any(mk in m["name"].lower() for mk in _UNDERGRAD_MARKERS)
            and not any(mk in m["name"].lower() for mk in _POSTGRAD_MARKERS)
        ]

        if len(undergrad) == 1:
            return undergrad

        if len(undergrad) > 1:
            # Prefer by standard Massey degree type order.
            _PREFERRED_ORDER = [
                "bachelor of science",
                "bachelor of arts",
                "bachelor of business",
                "bachelor of health science",
                "bachelor of engineering",
                "bachelor of information sciences",
                "bachelor of social work",
            ]
            for pref in _PREFERRED_ORDER:
                pref_match = [m for m in undergrad if pref in m["name"].lower()]
                if len(pref_match) == 1:
                    return pref_match

        return None  # Still ambiguous

    def _build_major_req_tree(self, major: dict) -> RequirementNode:
        """
        Parse the RequirementNode tree for this major's courses.

        The loaded dataset always provides a 'requirement' key containing a
        serialised node tree. The isinstance check on RequirementNode handles
        the case where the tree was already deserialised (e.g. in tests).
        """
        req = major.get("requirement")
        if isinstance(req, dict):
            return requirement_from_dict(req)
        if isinstance(req, RequirementNode):
            return req
        raise ValueError(
            f"Major '{major.get('name')}' has no 'requirement' field. "
            "Re-run ingestion to rebuild majors.json."
        )

    def _build_degree_tree(
        self,
        major: dict,
        major_req: RequirementNode,
        campus: str = "D",
        mode: str = "DIS",
    ) -> RequirementNode:
        """
        Derive the full degree requirement tree for this major, filtered to the
        configured delivery mode.

        Two adjustments are made relative to the raw requirement tree:

        1. CourseRequirement nodes for courses with no offering in the given
           campus/mode are removed. This aligns the validation tree with the
           working set so a plan is not penalised for requirements it genuinely
           cannot satisfy (e.g. internal-only fieldwork in a distance plan).

        2. Credit and level constraints (TotalCredits, MaxLevel, MinLevel) are
           included only when the schedulable major credits cover the full degree
           target. When the scraped data is incomplete (free electives missing),
           these constraints are omitted to avoid false failures.
        """

        name = major["name"]
        qual = self._qual_map.get(name)
        if qual is None:
            logger.warning("No qualification found for '%s'; using 360cr fallback.", name)
            return AllOfRequirement((TotalCreditsRequirement(360), major_req))

        all_codes = collect_course_codes(major_req)

        schedulable_codes = frozenset(
            code for code in all_codes
            if code in self.courses
            and any(o.campus == campus and o.mode == mode for o in self.courses[code].offerings)
        )

        course_credits = {code: self.courses[code].credits for code in self.courses}
        filtered_req = (
            filter_requirement_tree(major_req, schedulable_codes, course_credits)
            or major_req
        )

        schedulable_credits = sum(
            self.courses[c].credits for c in schedulable_codes
        )

        return build_degree_tree(
            major_req=filtered_req,
            qual_level=qual["level"],
            qual_length=qual["length"],
            major_name=name,
            schedulable_major_credits=schedulable_credits,
        )

    def generate_double_major_plan(
        self,
        major_name: str,
        second_major_name: str,
        max_credits_per_semester: int = 60,
        max_courses_per_semester: int | None = None,
        campus: str = "D",
        mode: str = "DIS",
        start_year: int = 2026,
        start_semester: str = "S1",
        prior_completed: frozenset = frozenset(),
        preferred_electives: frozenset = frozenset(),
        excluded_courses: frozenset = frozenset(),
        no_summer: bool = False,
        transfer_credits: int = 0,
    ) -> tuple["DegreePlan", dict]:
        """
        Generate a combined degree plan for two concurrent majors.

        Courses shared between majors are counted once (deduplication). The
        scheduler sees a single merged working set. Validation is run
        independently for each major so both requirement trees are satisfied.

        Returns (plan, info) where info contains:
            'shared_codes'    : frozenset of course codes counted in both majors
            'first_label'     : resolved name of the first major
            'second_label'    : resolved name of the second major
            'first_gap'       : free-elective gap for first major (credits)
            'second_gap'      : free-elective gap for second major (credits)
            'combined_credits': total credits in the merged requirement trees
                                (before deduplication)
            'saved_credits'   : credits saved by deduplication (shared × credit)

        Raises ValueError if either major cannot be resolved or no valid plan
        can be produced for the merged working set.
        """
        # Resolve both majors.
        first_resolved  = self._resolve_major(major_name)
        second_resolved = self._resolve_major(second_major_name)

        if len(first_resolved) != 1:
            raise ValueError(
                f"'{major_name}' must resolve to exactly one major for a double major plan."
            )
        if len(second_resolved) != 1:
            raise ValueError(
                f"'{second_major_name}' must resolve to exactly one major for a double major plan."
            )

        first_major  = first_resolved[0]
        second_major = second_resolved[0]

        first_req  = self._build_major_req_tree(first_major)
        second_req = self._build_major_req_tree(second_major)

        # Build merged requirement tree: ALL_OF(first_req, second_req).
        # Shared courses satisfy both subtrees - the validator checks each
        # independently against plan.all_course_codes.
        from coursemap.domain.requirement_nodes import AllOfRequirement as _AllOf
        merged_req = _AllOf((first_req, second_req))

        # Build per-major degree trees, then compose the merged tree using only
        # the course-requirement subtrees (not TotalCreditsRequirement nodes).
        # Using AllOf(first_tree, second_tree) directly would require 720cr total
        # for two 360cr degrees, which is incorrect: the two majors share a single
        # enrolment and the credit target is the higher of the two.
        first_tree  = self._build_degree_tree(first_major,  first_req,  campus=campus, mode=mode)
        second_tree = self._build_degree_tree(second_major, second_req, campus=campus, mode=mode)

        # Extract just the major-requirement subtrees (strip TotalCreditsRequirement
        # wrappers) so validation checks course coverage without demanding 720cr.
        def _major_subtree(tree: "RequirementNode") -> "RequirementNode":
            """Return the inner major requirement node, unwrapping AllOf(TotalCredits, req)."""
            from coursemap.domain.requirement_nodes import AllOfRequirement as _A, TotalCreditsRequirement as _T
            if isinstance(tree, _A) and len(tree.children) == 2:
                non_total = [c for c in tree.children if not isinstance(c, _T)]
                if len(non_total) == 1:
                    return non_total[0]
            return tree

        merged_tree = _AllOf((_major_subtree(first_tree), _major_subtree(second_tree)))

        parsed_combined = [{
            "name": f"{first_major['name']} + {second_major['name']}",
            "url":  "",
            "raw":  {},
            "requirement":  merged_req,
            "degree_tree":  merged_tree,
        }]

        generator_template = PlanGenerator(
            self.courses,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            start_semester=start_semester,
            no_summer=no_summer,
        )

        from coursemap.optimisation.search import PlanSearch as _PlanSearch
        search = _PlanSearch(
            courses=self.courses,
            majors=parsed_combined,
            generator_template=generator_template,
            prior_completed=prior_completed,
            preferred_electives=preferred_electives,
            excluded_courses=excluded_courses,
        )
        plan = search.search()
        self.last_plan_stats = search.best_generator_stats

        # Reattach prior-completed course objects.
        if prior_completed:
            prior_objects = tuple(
                self.courses[c] for c in prior_completed if c in self.courses
            )
            plan = type(plan)(semesters=plan.semesters, prior_completed=prior_objects)

        # Build info dict.
        from coursemap.domain.requirement_utils import collect_course_codes as _ccc
        first_codes  = _ccc(first_req)
        second_codes = _ccc(second_req)
        shared_codes = first_codes & second_codes

        saved_credits = sum(
            self.courses[c].credits for c in shared_codes if c in self.courses
        )
        combined_credits = sum(
            self.courses[c].credits
            for c in first_codes | second_codes
            if c in self.courses
        )

        info = {
            "shared_codes":     frozenset(shared_codes),
            "first_label":      first_major["name"],
            "second_label":     second_major["name"],
            "first_gap":        self.free_elective_gap(major_name,        campus=campus, mode=mode),
            "second_gap":       self.free_elective_gap(second_major_name, campus=campus, mode=mode),
            "combined_credits": combined_credits,
            "saved_credits":    saved_credits,
        }
        if transfer_credits > 0:
            plan.transfer_credits = transfer_credits
        return plan, info

    def generate_filled_plan(
        self,
        major_name: str | None = None,
        max_credits_per_semester: int = 60,
        max_courses_per_semester: int | None = None,
        campus: str = "D",
        mode: str = "DIS",
        start_year: int = 2026,
        start_semester: str = "S1",
        prior_completed: frozenset = frozenset(),
        preferred_electives: frozenset = frozenset(),
        excluded_courses: frozenset = frozenset(),
        no_summer: bool = False,
        transfer_credits: int = 0,
    ) -> tuple["DegreePlan", list[str]]:
        """
        Generate a plan and auto-fill the free-elective gap with subject-area courses.

        Returns (filled_plan, filler_codes) where filler_codes are the course
        codes added to fill the gap.  If the gap is zero the plan is returned
        unchanged with an empty filler list.

        The fill strategy selects same-subject-prefix courses ordered by
        (prefix_rank, level, code), then injects them into the major's requirement
        tree as an additional CHOOSE_CREDITS pool before re-running the full
        PlanSearch pipeline. This ensures the filled plan benefits from the same
        elective selection, prerequisite resolution, rebalancing and equalisation
        passes as a normal plan - fixing the sparse-final-semester issue that
        occurred when the filler bypassed PlanSearch.
        """
        from collections import Counter as _Counter
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement as _AllOf,
            ChooseCreditsRequirement as _CCR,
        )

        # First pass: generate the unfilled plan to discover working codes
        # and compute the free-elective gap.
        base_plan = self.generate_best_plan(
            major_name=major_name,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            start_semester=start_semester,
            prior_completed=prior_completed,
            preferred_electives=preferred_electives,
            excluded_courses=excluded_courses,
            no_summer=no_summer,
        )

        degree_total = self.degree_total_credits(major_name) if major_name else 360

        # Compute the true filler gap directly from the base plan's actual total.
        # base_plan.total_credits() already accounts for all prerequisite-chain
        # courses the scheduler actually placed - no need to estimate them separately.
        # This avoids the "overcounting" bug where prereq extras of unreachable courses
        # (e.g. a stats course needing a non-DIS maths prerequisite) were subtracted
        # from the budget even though they were never actually scheduled.
        base_scheduled = base_plan.total_credits() + base_plan.prior_credits()
        effective_gap = max(0, degree_total - base_scheduled - transfer_credits)

        if effective_gap <= 0:
            # Even if no filler is needed, the base plan may have scheduled more
            # than degree_total credits (prereq chains pulling in extra courses).
            # Apply the same trim logic to cap the plan at degree_total.
            base_total = base_plan.total_credits()
            if base_total > degree_total:
                _excess = base_total - degree_total
                _plan_codes = {c.code for s in base_plan.semesters for c in s.courses}
                _resolved_b = self._resolve_major(major_name) if major_name else []
                # Protect only explicitly REQUIRED courses (COURSE nodes).
                # Pool options (CHOOSE_CREDITS) that end up orphaned CAN be trimmed.
                _direct_codes_b: set = set()
                for _mr in _resolved_b:
                    _rr = _mr.get("raw", _mr).get("requirement", {})
                    def _rdc_req_only(r):
                        rc = set()
                        if r.get("type") == "COURSE":
                            c = r.get("course_code","")
                            if c: rc.add(c)
                        # Deliberately skip CHOOSE_CREDITS - pool options can be trimmed
                        for ch in r.get("children",[]): rc |= _rdc_req_only(ch)
                        return rc
                    _direct_codes_b |= _rdc_req_only(_rr)
                from coursemap.domain.prerequisite import (
                    CoursePrerequisite as _CPb, AndExpression as _ANDb, OrExpression as _ORb
                )
                _pn_b: set = set()
                for _code in _plan_codes:
                    _cc = self.courses.get(_code)
                    if _cc and _cc.prerequisites:
                        _stk = [_cc.prerequisites]
                        while _stk:
                            _n = _stk.pop()
                            if isinstance(_n, _CPb) and _n.code in _plan_codes:
                                _pn_b.add(_n.code)
                            elif isinstance(_n, (_ANDb, _ORb)):
                                _stk.extend(_n.children)
                _removable_b = sorted(
                    [c for c in _plan_codes if c not in _direct_codes_b and c not in _pn_b],
                    key=lambda c: self.courses[c].credits if c in self.courses else 0
                )
                _removed_b: set = set()
                for _rc in _removable_b:
                    if _excess <= 0:
                        break
                    _cr = self.courses[_rc].credits if _rc in self.courses else 0
                    if 0 < _cr <= _excess + 14:
                        _removed_b.add(_rc)
                        _excess -= _cr
                if _removed_b:
                    from coursemap.domain.plan import SemesterPlan as _SP2, DegreePlan as _DP2
                    _new_sems = tuple(
                        _SP2(year=s.year, semester=s.semester,
                             courses=tuple(c for c in s.courses if c.code not in _removed_b))
                        for s in base_plan.semesters
                        if any(c.code not in _removed_b for c in s.courses)
                    )
                    base_plan = _DP2(semesters=_new_sems,
                                     prior_completed=base_plan.prior_completed,
                                     transfer_credits=base_plan.transfer_credits)
            if transfer_credits > 0:
                base_plan.transfer_credits = transfer_credits
            return base_plan, []

        # Identify filler candidates in two passes:
        #   Pass 1 (same-prefix): courses whose 3-digit code prefix matches
        #           subjects already in the base plan. These are the most
        #           relevant free electives for the student's programme.
        #   Pass 2 (broadened): when pass 1 cannot cover the full gap, fall
        #           back to ANY undergrad-level course in the catalogue that
        #           is schedulable in the requested campus/mode.
        # Both passes respect the excluded_courses list and the per-course
        # level cap (no higher than max_level + 100 to stay broadly coherent).\
        planned_codes = {c.code for s in base_plan.semesters for c in s.courses}
        prior_codes   = {c.code for c in base_plan.prior_completed}

        # Also exclude codes that appear in the major's own elective pools.
        # When a pool code is also selected as a filler code it gets double-
        # counted: the scheduler places it once but both the pool requirement
        # and the filler pool requirement claim it, leaving the degree short
        # of the total-credit target.  Excluding pool codes from the filler
        # candidates prevents this overlap.
        resolved_for_pool = self._resolve_major(major_name) if major_name else []
        major_pool_codes: set[str] = set()
        for _m in resolved_for_pool:
            _rt = self._build_major_req_tree(_m)
            from coursemap.domain.requirement_utils import collect_elective_nodes as _cen
            for _node in _cen(_rt):
                if isinstance(_node, _CCR):
                    major_pool_codes.update(_node.course_codes)

        excluded_all  = planned_codes | prior_codes | excluded_courses | major_pool_codes

        prefix_counts = _Counter(code[:3] for code in planned_codes)
        prefix_rank   = {pfx: rank for rank, (pfx, _) in enumerate(prefix_counts.most_common())}
        max_level = max(
            (self.courses[c].level for c in planned_codes if c in self.courses),
            default=100,
        )

        def _is_schedulable(course) -> bool:
            offerings = [o for o in course.offerings if o.campus == campus and o.mode == mode]
            if not offerings:
                return False
            if no_summer:
                return any(o.semester in ("S1", "S2") for o in offerings)
            return True

        def _level_ok(course) -> bool:
            # Exclude zero-credit courses (practicum placements) - they can't
            # satisfy credit requirements and cause the filler loop to spin.
            if course.credits <= 0:
                return False
            return course.level <= 300 and course.level <= max_level + 100

        # Detect prerequisite-chain courses: needed to satisfy prerequisites
        # of major courses but not in the major requirement tree themselves.
        # These get higher priority than random electives in filler selection.
        prereq_chain_codes: set[str] = set()
        def _collect_prereqs_for_filler(code: str, visited: set[str]) -> None:
            if code in visited or code not in self.courses:
                return
            visited.add(code)
            from coursemap.domain.prerequisite import CoursePrerequisite, AndExpression, OrExpression
            prereq = self.courses[code].prerequisites
            if prereq is None:
                return
            stack = [prereq]
            while stack:
                node = stack.pop()
                if isinstance(node, CoursePrerequisite):
                    child = node.code
                    if child in self.courses and child not in excluded_all:
                        prereq_chain_codes.add(child)
                        _collect_prereqs_for_filler(child, visited)
                elif isinstance(node, (AndExpression, OrExpression)):
                    stack.extend(node.children)

        for code in list(planned_codes) + list(major_pool_codes):
            _collect_prereqs_for_filler(code, set())

        # Priority tiers for filler:
        #   0 = student explicitly preferred
        #   1 = prerequisite chain courses (pedagogically necessary)
        #   2 = same subject prefix (most coherent electives)
        #   3 = full undergrad catalogue (open electives)
        def _filler_tier(code: str) -> int:
            if code in preferred_electives: return 0
            if code in prereq_chain_codes: return 1
            if code[:3] in prefix_rank: return 2
            return 3

        all_candidates: list[tuple] = []
        for code, course in self.courses.items():
            if code in excluded_all: continue
            if not _is_schedulable(course): continue
            if not _level_ok(course): continue
            tier = _filler_tier(code)
            sub_rank = prefix_rank.get(code[:3], 99)
            all_candidates.append((tier, sub_rank, course.level, code))
        all_candidates.sort()

        filler_codes: list[str] = []
        running = 0
        for _, _, _, code in all_candidates:
            if running >= effective_gap: break
            cr = self.courses[code].credits
            if running + cr > effective_gap + 14: continue
            filler_codes.append(code)
            running += cr

        if not filler_codes:
            if transfer_credits > 0:
                base_plan.transfer_credits = transfer_credits
            return base_plan, []

        # Second pass: inject filler codes as a new CHOOSE_CREDITS pool appended
        # to the major's requirement tree, then re-run PlanSearch end-to-end.
        # This preserves full elective selection, prerequisite ordering, and all
        # four rebalancing/equalisation passes for the combined course set.
        filler_pool = _CCR(
            credits=running,
            course_codes=frozenset(filler_codes),
        )
        resolved = self._resolve_major(major_name)

        parsed_majors_filled = []
        for m in resolved:
            req_tree = self._build_major_req_tree(m)
            # Attach filler pool alongside the major's own requirements.
            augmented_req = _AllOf((req_tree, filler_pool))

            # Build the degree tree from augmented_req so TotalCreditsRequirement
            # accounts for the filler credits. Without this, PlanSearch computes
            # schedulable_pool_contribution including the filler 150cr, then caps
            # required_codes to degree_total - pool_contribution = 210cr, which
            # truncates the 210cr major working set to 150cr.
            #
            # By passing augmented_req (210cr required + 150cr filler pool = 360cr)
            # as schedulable_major_credits, build_degree_tree includes
            # TotalCreditsRequirement(360) and PlanSearch treats the full 360cr
            # as the intended plan size.
            name_m = m["name"]
            qual = self._qual_map.get(name_m)
            if qual is not None:
                from coursemap.domain.requirement_utils import collect_course_codes as _ccc
                from coursemap.rules.degree_rules import profile_for as _pfor, filter_requirement_tree as _frt
                all_aug_codes = _ccc(augmented_req)
                schedulable_aug = frozenset(
                    code for code in all_aug_codes
                    if code in self.courses
                    and any(o.campus == campus and o.mode == mode for o in self.courses[code].offerings)
                )
                schedulable_aug_credits = sum(self.courses[c].credits for c in schedulable_aug)
                filtered_aug = _frt(augmented_req, schedulable_aug, {c: self.courses[c].credits for c in self.courses}) or augmented_req
                from coursemap.rules.degree_rules import build_degree_tree as _bdt
                # force_total_credits=True because the augmented tree now
                # includes filler courses that bring the working set to the
                # degree credit target, so PlanSearch must schedule all of them.
                degree_tree = _bdt(
                    major_req=filtered_aug,
                    qual_level=qual["level"],
                    qual_length=qual["length"],
                    major_name=name_m,
                    schedulable_major_credits=schedulable_aug_credits,
                    force_total_credits=True,
                )
            else:
                degree_tree = self._build_degree_tree(m, req_tree, campus=campus, mode=mode)

            parsed_majors_filled.append({
                "name":         m["name"],
                "url":          m.get("url", ""),
                "raw":          m,
                "requirement":  augmented_req,
                "degree_tree":  degree_tree,
            })

        generator_template = PlanGenerator(
            self.courses,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            start_semester=start_semester,
            no_summer=no_summer,
        )

        # preferred_electives for the filled pass includes filler codes so
        # PlanSearch's _select_electives picks them from the new pool.
        augmented_preferred = preferred_electives | frozenset(filler_codes)

        search = PlanSearch(
            courses=self.courses,
            majors=parsed_majors_filled,
            generator_template=generator_template,
            prior_completed=prior_completed,
            preferred_electives=augmented_preferred,
            excluded_courses=excluded_courses,
        )

        try:
            filled_plan = search.search()
        except ValueError:
            # Filling failed. If the shortfall is significant (the base plan is
            # substantially under the degree target), raise ValueError so the caller
            # can try a different campus/mode combination. This prevents the batch
            # planner from reporting a 225/360cr plan as a "result" for a major that
            # is simply not completable via the requested campus/mode.
            base_total_check = base_plan.total_credits() + base_plan.prior_credits()
            shortfall = degree_total - base_total_check - transfer_credits
            if shortfall > 15:
                raise ValueError(
                    f"Cannot complete '{major_name or 'degree'}' to {degree_total}cr "
                    f"via {campus}/{mode}: only {base_total_check}cr schedulable "
                    f"({shortfall}cr short). Try a different campus or delivery mode."
                )
            if transfer_credits > 0:
                base_plan.transfer_credits = transfer_credits
            return base_plan, filler_codes

        self.last_plan_stats = search.best_generator_stats

        # Post-process: trim to degree_total if the second pass over-scheduled.
        # PlanGenerator schedules everything in the working set; the prereq
        # expansion and pool selection may push the total above degree_total.
        # Remove excess courses (prefer removing non-preferred electives first).
        filled_total = sum(
            sum(c.credits for c in s.courses) for s in filled_plan.semesters
        )
        if filled_total > degree_total:
            excess = filled_total - degree_total
            filler_set = set(filler_codes)

            # Only trim FILLER codes (never major tree or prereq-chain courses).
            # Remove from the end of filler_codes (last-selected = lowest priority).
            # Never remove a course that is a direct prerequisite of another
            # course remaining in the plan.
            plan_codes = {c.code for s in filled_plan.semesters for c in s.courses}
            prereqs_needed: set[str] = set()
            from coursemap.domain.prerequisite import (
                CoursePrerequisite as _CP2,
                AndExpression as _AND2,
                OrExpression as _OR2,
            )
            for s in filled_plan.semesters:
                for c in s.courses:
                    if c.prerequisites is None:
                        continue
                    stack = [c.prerequisites]
                    while stack:
                        n = stack.pop()
                        if isinstance(n, _CP2) and n.code in plan_codes:
                            prereqs_needed.add(n.code)
                        elif isinstance(n, (_AND2, _OR2)):
                            stack.extend(n.children)

            # Protect only explicitly REQUIRED courses (COURSE nodes), not pool options.
            # Pool options that end up orphaned (nobody depends on them) can be trimmed.
            major_tree_codes: set[str] = set()
            def _collect_required_codes(req_dict: dict) -> set:
                codes: set = set()
                t = req_dict.get("type", "")
                if t == "COURSE":
                    c = req_dict.get("course_code", "")
                    if c: codes.add(c)
                # Deliberately skip CHOOSE_CREDITS pool codes - orphaned pool
                # selections can be trimmed to hit the degree credit target.
                for ch in req_dict.get("children", []):
                    codes |= _collect_required_codes(ch)
                return codes
            for _pm in parsed_majors_filled:
                _raw_req = _pm.get("raw", {}).get("requirement", {})
                major_tree_codes |= _collect_required_codes(_raw_req)
                # Also include filler_codes themselves (they're intentionally selected)
                major_tree_codes |= filler_set

            removable = [
                code for code in reversed(filler_codes)
                if code not in prereqs_needed
                and code not in preferred_electives
            ]

            # Phase 2: if still over-budget, also remove orphaned prereq-extras
            # (courses not in major tree, not needed by anyone as a prereq).
            if excess > 0:
                removable_extras = [
                    code for code in sorted(plan_codes)
                    if code not in major_tree_codes
                    and code not in prereqs_needed
                    and code not in filler_set
                    and code not in preferred_electives
                    and code not in removable
                ]
                removable = removable + removable_extras

            removed: set[str] = set()
            for code in removable:
                if excess <= 0:
                    break
                cr = self.courses[code].credits if code in self.courses else 0
                if cr > 0 and cr <= excess + 14:
                    removed.add(code)
                    excess -= cr
                    # After removing this course, some of its prereqs may now be
                    # orphaned - add them to the removable list if they're not
                    # needed by any remaining course.
                    still_in_plan = plan_codes - removed
                    new_needed: set[str] = set()
                    for _c in still_in_plan:
                        _course = self.courses.get(_c)
                        if _course and _course.prerequisites:
                            _stk = [_course.prerequisites]
                            while _stk:
                                _n = _stk.pop()
                                if isinstance(_n, _CP2) and _n.code in still_in_plan:
                                    new_needed.add(_n.code)
                                elif isinstance(_n, (_AND2, _OR2)):
                                    _stk.extend(_n.children)
                    newly_orphaned = [
                        c for c in still_in_plan
                        if c not in major_tree_codes
                        and c not in new_needed
                        and c not in removed
                        and c not in preferred_electives
                        and c not in removable
                    ]
                    if newly_orphaned:
                        removable = removable + newly_orphaned
                        prereqs_needed = new_needed

            if removed:
                from coursemap.domain.plan import SemesterPlan
                new_sems = []
                for sem in filled_plan.semesters:
                    new_courses = [c for c in sem.courses if c.code not in removed]
                    if new_courses:
                        new_sems.append(SemesterPlan(
                            year=sem.year,
                            semester=sem.semester,
                            courses=tuple(new_courses),
                        ))
                filled_plan = type(filled_plan)(
                    semesters=tuple(new_sems),
                    prior_completed=filled_plan.prior_completed,
                    transfer_credits=filled_plan.transfer_credits,
                )
                filler_codes = [c for c in filler_codes if c not in removed]

        if transfer_credits > 0:
            filled_plan.transfer_credits = transfer_credits
        return filled_plan, filler_codes

    def degree_total_credits(
        self,
        major_name: str,
    ) -> int:
        """
        Return the total credit target for the degree associated with this major.

        Uses the qualification's level and length to look up the standard Massey
        credit profile. Falls back to 360 when qualification metadata is missing.
        """
        resolved = self._resolve_major(major_name)
        if not resolved:
            return 360
        qual = self._qual_map.get(resolved[0]["name"])
        if qual is None:
            return 360
        return profile_for(qual["level"], qual["length"]).total_credits

    def free_elective_gap(
        self,
        major_name: str,
        campus: str = "D",
        mode: str = "DIS",
    ) -> int:
        """
        Return the number of additional free-elective credits needed beyond the
        major-specific courses captured in the dataset.

        Many majors include fewer credits in the scraped data than the full
        degree total because free electives (unrestricted course choices) are
        not listed on the major page. A positive return value means the student
        must self-select that many credits from the wider Massey catalogue.

        The calculation uses the minimum schedulable credits from the requirement
        tree: required course credits + elective pool *targets* (not the sum of
        all alternatives in a pool, which would overcount).

        Returns 0 when the major data fully covers the degree credit target.
        """
        resolved = self._resolve_major(major_name)
        if not resolved:
            return 0
        major = resolved[0]
        name  = major["name"]
        qual  = self._qual_map.get(name)
        if qual is None:
            return 0

        major_req = self._build_major_req_tree(major)

        # Collect per-pool credit targets (not all members) for CHOOSE_CREDITS nodes.
        elective_nodes = collect_elective_nodes(major_req)
        pool_codes: set[str] = {c for n in elective_nodes for c in n.course_codes}

        # Required (non-pool) course credits that are schedulable.
        all_codes   = collect_course_codes(major_req)
        req_codes   = all_codes - pool_codes
        req_credits = sum(
            self.courses[c].credits
            for c in req_codes
            if c in self.courses
            and any(o.campus == campus and o.mode == mode for o in self.courses[c].offerings)
        )

        # Pool contribution = min(pool.credits target, schedulable pool member credits).
        pool_credits = 0
        for node in elective_nodes:
            schedulable_in_pool = sum(
                self.courses[c].credits
                for c in node.course_codes
                if c in self.courses
                and any(o.campus == campus and o.mode == mode for o in self.courses[c].offerings)
            )
            target = node.credits if node.credits > 0 else schedulable_in_pool
            pool_credits += min(target, schedulable_in_pool)

        schedulable = req_credits + pool_credits
        profile = profile_for(qual["level"], qual["length"])
        return max(0, profile.total_credits - schedulable)

    def campus_excluded_courses(
        self,
        major_name: str,
        campus: str = "D",
        mode: str = "DIS",
    ) -> list[str]:
        """
        Return course codes that are required by this major but have no offering
        in the given campus/mode.

        These are courses the student cannot take by distance (or at the specified
        campus) and which are excluded from both scheduling and validation. The CLI
        uses this to inform the student that some requirements need on-campus study.
        """
        resolved = self._resolve_major(major_name)
        if not resolved:
            return []
        major = resolved[0]
        major_req = self._build_major_req_tree(major)
        all_codes = collect_course_codes(major_req)
        pool_codes = {
            c
            for n in collect_elective_nodes(major_req)
            if isinstance(n, ChooseCreditsRequirement)
            for c in n.course_codes
        }
        required_codes = all_codes - pool_codes
        return sorted(
            code for code in required_codes
            if code in self.courses
            and not any(o.campus == campus and o.mode == mode for o in self.courses[code].offerings)
        )

    # ------------------------------------------------------------------
    # Elective filler - shared by both single and double major fill logic
    # ------------------------------------------------------------------

    def _select_filler_codes(
        self,
        planned_codes: set[str],
        prior_codes: set[str],
        effective_gap: int,
        campus: str,
        mode: str,
        no_summer: bool,
        preferred_electives: frozenset,
        excluded_courses: frozenset,
        extra_exclude: set[str] | None = None,
    ) -> list[str]:
        """
        Delegate elective selection to ElectiveFiller.

        Returns a list of course codes whose total credits <= effective_gap.
        Uses the same tier-ranked strategy (preferred > same-prefix > adjacent)
        for both single and double major plans - eliminating the duplication
        between generate_filled_plan and generate_filled_double_major_plan.
        """
        exclude_all = planned_codes | prior_codes | excluded_courses | (extra_exclude or set())
        available = frozenset(planned_codes | prior_codes)
        # Max level cap: don't recommend courses 2+ levels above what's in the plan
        max_level = max(
            (self.courses[c].level for c in planned_codes if c in self.courses),
            default=100,
        )
        level_cap = max_level + 200  # generous cap

        filler = ElectiveFiller(self.courses, campus=campus, mode=mode)
        selected = filler.select_to_fill(
            seed_codes=list(planned_codes),
            completed=available,
            budget_credits=effective_gap,
            exclude=frozenset(exclude_all),
            prefer=frozenset(preferred_electives),
            level_cap=level_cap,
        )

        # Pass 2 (broadened): if Pass 1 didn't fill the budget, try any undergrad
        # DIS course in the catalogue. This is essential for narrow majors (e.g.
        # language degrees) where same-prefix electives are exhausted.
        selected_cr = sum(self.courses[c].credits for c in selected if c in self.courses)
        if selected_cr < effective_gap:
            remaining_budget = effective_gap - selected_cr
            already_selected = frozenset(selected)
            broader_filler = ElectiveFiller(self.courses, campus=campus, mode=mode)
            # Use empty seed_codes so no prefix is "top" - all undergrad courses are candidates
            broader = broader_filler.select_to_fill(
                seed_codes=[],
                completed=available | already_selected,
                budget_credits=remaining_budget,
                exclude=frozenset(exclude_all) | already_selected,
                prefer=frozenset(preferred_electives),
                level_cap=min(level_cap, 400),
            )
            selected = selected + broader

        return selected

    def generate_filled_double_major_plan(
        self,
        major_name: str,
        second_major_name: str,
        max_credits_per_semester: int = 60,
        max_courses_per_semester: int | None = None,
        campus: str = "D",
        mode: str = "DIS",
        start_year: int = 2026,
        start_semester: str = "S1",
        prior_completed: frozenset = frozenset(),
        preferred_electives: frozenset = frozenset(),
        excluded_courses: frozenset = frozenset(),
        no_summer: bool = False,
        transfer_credits: int = 0,
    ) -> tuple["DegreePlan", dict, list[str]]:
        """
        Generate a combined double-major plan and auto-fill the free-elective gap.

        The combined gap is calculated as:
            max(first_degree_total, second_degree_total)
            - schedulable credits from merged requirement tree

        This ensures the student meets the higher degree's credit requirement
        (typically both are 360cr) while counting shared courses only once.

        Returns (plan, double_info, filler_codes).
        """
        from coursemap.domain.requirement_nodes import (
            AllOfRequirement as _AllOf,
            ChooseCreditsRequirement as _CCR,
        )
        from coursemap.optimisation.search import PlanSearch as _PlanSearch

        # --- Step 1: generate the unfilled double-major plan -----------------
        base_plan, info = self.generate_double_major_plan(
            major_name=major_name,
            second_major_name=second_major_name,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            prior_completed=prior_completed,
            preferred_electives=preferred_electives,
            excluded_courses=excluded_courses,
            no_summer=no_summer,
            transfer_credits=transfer_credits,
        )

        # --- Step 2: compute gap ---------------------------------------------
        first_total  = self.degree_total_credits(major_name)
        second_total = self.degree_total_credits(second_major_name)
        degree_target = max(first_total, second_total)

        credits_planned = base_plan.total_credits() + base_plan.prior_credits()
        gap = max(0, degree_target - credits_planned)
        effective_gap = max(0, gap - transfer_credits)

        if effective_gap <= 0:
            # Even when no filler is needed, prereq expansion may have
            # pushed the base plan over degree_target. Use the search's
            # internal trim (via _plan_for_major) if the plan is over.
            # Since we return base_plan directly here, apply a quick trim.
            base_total_cr = sum(sum(c.credits for c in s.courses) for s in base_plan.semesters)
            if base_total_cr > degree_target:
                excess = base_total_cr - degree_target
                plan_codes_set = {c.code for s in base_plan.semesters for c in s.courses}
                from coursemap.domain.prerequisite import (
                    CoursePrerequisite as _CPd,
                    AndExpression as _ANDd,
                    OrExpression as _ORd,
                )
                prereqs_needed_d: set[str] = set()
                for s in base_plan.semesters:
                    for c in s.courses:
                        if c.prerequisites is None: continue
                        stk = [c.prerequisites]
                        while stk:
                            n = stk.pop()
                            if isinstance(n, _CPd) and n.code in plan_codes_set:
                                prereqs_needed_d.add(n.code)
                            elif isinstance(n, (_ANDd, _ORd)):
                                stk.extend(n.children)
                from coursemap.domain.requirement_utils import collect_course_codes as _ccc2, collect_elective_nodes as _cen3
                pool_codes_d: set[str] = set()
                req_codes_d: set[str] = set()
                for _mn in [major_name, second_major_name]:
                    _rx = self._resolve_major(_mn)
                    if not _rx: continue
                    _rt2 = self._build_major_req_tree(_rx[0])
                    _all_t = _ccc2(_rt2)
                    _pool_t = {c for n in _cen3(_rt2) if isinstance(n, _CCR) for c in n.course_codes}
                    pool_codes_d.update(_pool_t)
                    req_codes_d.update(_all_t - _pool_t)
                # Removable: pool courses not in required_codes/always_include
                removable_d = sorted(
                    [c for s in base_plan.semesters for c in s.courses
                     if c.code in pool_codes_d and c.code not in req_codes_d
                     and c.code not in prereqs_needed_d],
                    key=lambda c: (-c.level, c.code),
                )
                removed_d: set[str] = set()
                for cand in removable_d:
                    if excess <= 0: break
                    if cand.code not in removed_d:
                        removed_d.add(cand.code)
                        excess -= cand.credits
                if removed_d:
                    from coursemap.domain.plan import SemesterPlan
                    new_sems = []
                    for sem in base_plan.semesters:
                        nc = [c for c in sem.courses if c.code not in removed_d]
                        if nc:
                            new_sems.append(SemesterPlan(year=sem.year, semester=sem.semester, courses=tuple(nc)))
                    base_plan = type(base_plan)(semesters=tuple(new_sems), prior_completed=base_plan.prior_completed, transfer_credits=base_plan.transfer_credits)
            return base_plan, info, []

        # --- Step 3: find filler candidates ----------------------------------
        # Delegate to _select_filler_codes which uses ElectiveFiller under the hood.
        # Exclude codes in either major's own elective pools to prevent double-counting.
        planned_codes = {c.code for s in base_plan.semesters for c in s.courses}
        prior_codes   = {c.code for c in base_plan.prior_completed}

        first_resolved_tmp  = self._resolve_major(major_name)
        second_resolved_tmp = self._resolve_major(second_major_name)
        dm_pool_codes: set[str] = set()
        for _mr in [first_resolved_tmp[0], second_resolved_tmp[0]]:
            _rt = self._build_major_req_tree(_mr)
            from coursemap.domain.requirement_utils import collect_elective_nodes as _cen2
            for _node in _cen2(_rt):
                if isinstance(_node, _CCR):
                    dm_pool_codes.update(_node.course_codes)

        filler_codes = self._select_filler_codes(
            planned_codes=planned_codes,
            prior_codes=prior_codes,
            effective_gap=effective_gap,
            campus=campus,
            mode=mode,
            no_summer=no_summer,
            preferred_electives=preferred_electives,
            excluded_courses=excluded_courses,
            extra_exclude=(dm_pool_codes & planned_codes),  # only exclude already-planned pool codes
        )
        running = sum(self.courses[c].credits for c in filler_codes if c in self.courses)

        if not filler_codes:
            return base_plan, info, []

        # --- Step 4: re-run the double-major plan with filler injected -------
        first_resolved  = self._resolve_major(major_name)
        second_resolved = self._resolve_major(second_major_name)
        first_major  = first_resolved[0]
        second_major = second_resolved[0]

        first_req  = self._build_major_req_tree(first_major)
        second_req = self._build_major_req_tree(second_major)

        filler_pool = _CCR(credits=running, course_codes=frozenset(filler_codes))
        merged_req = _AllOf((first_req, second_req, filler_pool))

        def _major_subtree(tree: "RequirementNode") -> "RequirementNode":
            from coursemap.domain.requirement_nodes import AllOfRequirement as _A, TotalCreditsRequirement as _T
            if isinstance(tree, _A) and len(tree.children) == 2:
                non_total = [c for c in tree.children if not isinstance(c, _T)]
                if len(non_total) == 1:
                    return non_total[0]
            return tree

        first_tree  = self._build_degree_tree(first_major,  first_req,  campus=campus, mode=mode)
        second_tree = self._build_degree_tree(second_major, second_req, campus=campus, mode=mode)

        # Add TotalCreditsRequirement for the combined plan so PlanSearch
        # schedules up to the degree target (typically 360cr).
        from coursemap.rules.degree_rules import TotalCreditsRequirement as _TCR
        merged_tree = _AllOf((
            _TCR(degree_target),
            _major_subtree(first_tree),
            _major_subtree(second_tree),
        ))

        parsed_combined = [{
            "name": f"{first_major['name']} + {second_major['name']}",
            "url":  "",
            "raw":  {},
            "requirement":  merged_req,
            "degree_tree":  merged_tree,
        }]

        generator_template = PlanGenerator(
            self.courses,
            max_credits_per_semester=max_credits_per_semester,
            max_courses_per_semester=max_courses_per_semester,
            campus=campus,
            mode=mode,
            start_year=start_year,
            start_semester=start_semester,
            no_summer=no_summer,
        )

        augmented_preferred = preferred_electives | frozenset(filler_codes)
        search = _PlanSearch(
            courses=self.courses,
            majors=parsed_combined,
            generator_template=generator_template,
            prior_completed=prior_completed,
            preferred_electives=augmented_preferred,
            excluded_courses=excluded_courses,
        )

        try:
            filled_plan = search.search()
        except ValueError:
            # If the shortfall is significant, raise ValueError so callers can
            # try a different campus/mode (e.g. D/DIS → M/INT).
            base_scheduled = base_plan.total_credits() + base_plan.prior_credits()
            shortfall = degree_target - base_scheduled - transfer_credits
            if shortfall > 15:
                raise ValueError(
                    f"Cannot complete double major '{major_name or ''} + {second_major_name or ''}' "
                    f"to {degree_target}cr via {campus}/{mode}: only {base_scheduled}cr schedulable "
                    f"({shortfall}cr short). Check that all required courses have {campus}/{mode} offerings."
                )
            return base_plan, info, filler_codes

        self.last_plan_stats = search.best_generator_stats

        if prior_completed:
            prior_objects = tuple(
                self.courses[c] for c in prior_completed if c in self.courses
            )
            filled_plan = type(filled_plan)(
                semesters=filled_plan.semesters,
                prior_completed=prior_objects,
            )

        if transfer_credits > 0:
            filled_plan.transfer_credits = transfer_credits

        # Post-process: trim to degree_total if overcrediting from prereq expansion.
        # Only remove filler codes not needed as direct prerequisites.
        filled_dm_total = sum(
            sum(c.credits for c in s.courses) for s in filled_plan.semesters
        )
        if filled_dm_total > degree_target:
            dm_excess = filled_dm_total - degree_target
            dm_filler_set = set(filler_codes)
            dm_plan_codes = {c.code for s in filled_plan.semesters for c in s.courses}
            from coursemap.domain.prerequisite import (
                CoursePrerequisite as _CP4,
                AndExpression as _AND4,
                OrExpression as _OR4,
            )
            dm_prereqs_needed: set[str] = set()
            for s in filled_plan.semesters:
                for c in s.courses:
                    if c.prerequisites is None:
                        continue
                    stack = [c.prerequisites]
                    while stack:
                        n = stack.pop()
                        if isinstance(n, _CP4) and n.code in dm_plan_codes:
                            dm_prereqs_needed.add(n.code)
                        elif isinstance(n, (_AND4, _OR4)):
                            stack.extend(n.children)
            dm_removable = [
                code for code in reversed(filler_codes)
                if code not in dm_prereqs_needed
                and code not in preferred_electives
            ]
            dm_removed: set[str] = set()
            for code in dm_removable:
                if dm_excess <= 0:
                    break
                cr = self.courses[code].credits if code in self.courses else 0
                if cr > 0 and cr <= dm_excess + 14:
                    dm_removed.add(code)
                    dm_excess -= cr
            if dm_removed:
                from coursemap.domain.plan import SemesterPlan
                new_sems = []
                for sem in filled_plan.semesters:
                    nc = [c for c in sem.courses if c.code not in dm_removed]
                    if nc:
                        new_sems.append(SemesterPlan(
                            year=sem.year, semester=sem.semester, courses=tuple(nc)
                        ))
                filled_plan = type(filled_plan)(
                    semesters=tuple(new_sems),
                    prior_completed=filled_plan.prior_completed,
                    transfer_credits=filled_plan.transfer_credits,
                )
                filler_codes = [c for c in filler_codes if c not in dm_removed]

        return filled_plan, info, filler_codes

    def student_excluded_required_courses(
        self,
        major_name: str,
        excluded_courses: frozenset,
        campus: str = "D",
        mode: str = "DIS",
    ) -> list[str]:
        """
        Return required course codes that the student has excluded via --exclude
        but which are mandatory for the degree (COURSE nodes, not pool members).

        The CLI uses this to warn students that their exclusions conflict with
        degree requirements, rather than letting the plan silently fail validation.
        """
        resolved = self._resolve_major(major_name)
        if not resolved:
            return []
        major = resolved[0]
        major_req = self._build_major_req_tree(major)
        all_codes = collect_course_codes(major_req)
        pool_codes = {
            c
            for n in collect_elective_nodes(major_req)
            if isinstance(n, ChooseCreditsRequirement)
            for c in n.course_codes
        }
        required_codes = all_codes - pool_codes
        return sorted(code for code in excluded_courses if code in required_codes)
