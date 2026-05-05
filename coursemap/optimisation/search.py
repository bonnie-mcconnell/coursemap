"""
Plan search: selects electives and evaluates degree plans.

The central challenge is elective selection: a student must choose N credits
from a pool of M courses, and we want to find the combination that produces
the most compact, balanced plan.

Previous approach: exhaustive enumeration of all valid combinations via
itertools.product. This is exact but exponential -- C(M, N/15) combinations
per pool, multiplied across pools. With real credit values (e.g. choose 45cr
from 10 courses) the search space is C(10,3) = 120 per pool; with three pools
that is 1.7M combinations before even running the scheduler.

Current approach: greedy minimum-credit selection per pool, with student
preference signals. For each pool we select the fewest courses that meet the
credit requirement, preferring:
  1. Courses the student has indicated a preference for (--prefer flag).
  2. Courses offered in the earliest possible semester (minimises waiting time).
  3. Lower-level courses before higher-level (foundation before specialisation).
  4. Alphabetical by code as a final tiebreaker (determinism).

This is O(M log M) per pool and produces a single candidate plan rather than
enumerating the full search space. It is correct for the vast majority of
cases; the rare scenario where the greedy selection creates an unsatisfiable
prerequisite chain is handled by falling back to an alternative ordering.

The search still iterates over all matching majors when the user provides a
partial name, picking the major whose plan has the best score.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable

from coursemap.domain.course import Course
from coursemap.domain.plan import DegreePlan
from coursemap.domain.requirement_nodes import (
    AllOfRequirement,
    AnyOfRequirement,
    ChooseCreditsRequirement,
    MajorRequirement,
    RequirementNode,
)
from coursemap.domain.requirement_nodes import CourseRequirement as CourseReq
from coursemap.domain.requirement_utils import (
    collect_course_codes,
    collect_elective_nodes,
    find_total_credits,
)
from coursemap.domain.prerequisite_utils import prereqs_met
from coursemap.planner.generator import PlanGenerator
from coursemap.optimisation.scorer import PlanScorer
from coursemap.validation.engine import DegreeValidator
from coursemap.rules.degree_rules import filter_requirement_tree as _frt

logger = logging.getLogger(__name__)

_scorer = PlanScorer()


def _collect_course_node_codes(node: RequirementNode) -> set[str]:
    """
    Collect codes from COURSE requirement nodes only, ignoring pool members.

    Unlike collect_course_codes, this does not recurse into ChooseCreditsRequirement
    nodes. Used to find codes that are individually required (COURSE nodes) even
    when those same codes also appear in elective pools.
    """
    if isinstance(node, CourseReq):
        return {node.course_code}
    if isinstance(node, (AllOfRequirement, AnyOfRequirement)):
        result: set[str] = set()
        for child in node.children:
            result |= _collect_course_node_codes(child)
        return result
    if isinstance(node, MajorRequirement):
        return _collect_course_node_codes(node.requirement)
    return set()


class PlanSearch:
    """
    Selects electives and finds the best valid degree plan for each major.

    Attributes:
        courses:             Full course catalogue.
        majors:              List of major dicts from PlannerService, each containing
                             'name', 'raw', 'requirement', 'degree_tree' fields.
        generator_template:  PlanGenerator instance used as a settings template.
        prior_completed:     Course codes completed before this plan.
        preferred_electives: Course codes the student wants to prioritise.
    """

    def __init__(
        self,
        courses: dict[str, Course],
        majors: list[dict],
        generator_template: PlanGenerator,
        prior_completed: frozenset[str] = frozenset(),
        preferred_electives: frozenset[str] = frozenset(),
        excluded_courses: frozenset[str] = frozenset(),
    ):
        self.courses = courses
        self.majors = majors
        self.generator_template = generator_template
        self.prior_completed = prior_completed
        self.preferred_electives = preferred_electives
        self.excluded_courses = excluded_courses
        self.best_generator_stats = None

        # Diagnostics
        self.attempts = 0
        self.failures: dict[str, int] = {}

    def search(self) -> DegreePlan:
        """
        Return the best valid plan found across all candidate majors.

        Raises ValueError if no valid plan can be constructed for any major.
        """
        best_plan: DegreePlan | None = None
        best_score: float | None = None
        best_generator: PlanGenerator | None = None

        for major in self.majors:
            try:
                plan, generator = self._plan_for_major(major)
            except Exception as exc:
                name = major.get("name", "?")
                logger.debug("Major '%s' failed: %s", name, exc)
                self.failures[name] = str(exc)
                continue

            self.attempts += 1
            score = _scorer.score(plan)
            if best_score is None or score < best_score:
                best_plan = plan
                best_score = score
                best_generator = generator

        if best_plan is None:
            detail = "; ".join(
                f"'{k}': {v}" for k, v in list(self.failures.items())[:3]
            )
            raise ValueError(f"No valid plan found. {detail}")

        self.best_generator_stats = best_generator.stats if best_generator else None
        return best_plan

    # ------------------------------------------------------------------
    # Per-major planning
    # ------------------------------------------------------------------

    def _plan_for_major(self, major: dict) -> tuple[DegreePlan, PlanGenerator]:
        """
        Build and validate a plan for one major.

        Steps:
          1. Collect required course codes (COURSE nodes only, not pool members).
          2. Select elective courses from each pool using the greedy strategy.
          3. Remove any courses already completed by the student.
          4. Run the scheduler.
          5. Validate against the degree tree.
          6. Attach prior-completed course objects to the plan.
        """
        degree_tree: RequirementNode = major["degree_tree"]
        major_req: RequirementNode = major["requirement"]
        name: str = major["name"]

        # Step 1: separate required (COURSE nodes) from elective pool codes.
        # collect_course_codes returns ALL codes in the tree including pool members.
        # We only schedule pool members that the student actually selects.
        elective_nodes = [
            n for n in collect_elective_nodes(major_req)
            if isinstance(n, ChooseCreditsRequirement)
        ]
        pool_codes: set[str] = {code for n in elective_nodes for code in n.course_codes}
        all_major_codes = collect_course_codes(major_req)
        required_codes = all_major_codes - pool_codes

        # Step 1b: cap required_codes at the degree credit target.
        #
        # The scraper sometimes over-captures: for a 120cr Honours degree it
        # may record every course in a subject department as required. The degree
        # tree carries the correct target via TotalCreditsRequirement.
        #
        # When required_codes credits exceed the degree target, we trim to the
        # minimum needed, but we MUST keep every code that the (filtered) degree
        # tree explicitly checks as a COURSE node -- dropping those would fail
        # validation. filter_requirement_tree already stripped unschedulable
        # courses, so degree-tree COURSE nodes are exactly what must be planned.
        degree_total = find_total_credits(degree_tree)
        campus = self.generator_template.campus
        mode   = self.generator_template.mode

        required_was_capped = False
        if degree_total > 0:
            req_total = sum(
                self.courses[c].credits for c in required_codes if c in self.courses
            )
            # Compute the pool's schedulable credit contribution (DIS only),
            # capped at each pool's stated target. This is what the pools will
            # actually contribute to the plan after _select_electives runs.
            schedulable_pool_contribution = sum(
                min(
                    n.credits,
                    sum(
                        self.courses[c].credits for c in n.course_codes
                        if c in self.courses
                        and any(o.campus == campus and o.mode == mode
                                for o in self.courses[c].offerings)
                    ),
                )
                for n in elective_nodes
            )

            # Cap required_codes when required + actual pool contribution overflow
            # the degree total. We reserve exactly what pools can deliver so pools
            # are always satisfiable at their full (DIS-capped) target, and required
            # courses fill the remainder.
            req_budget = (
                max(0, degree_total - schedulable_pool_contribution)
                if req_total + schedulable_pool_contribution > degree_total
                else degree_total
            )
            if req_total > req_budget:
                required_was_capped = True
                # Priority order: schedulable > non-schedulable, lower-level > higher,
                # in-degree-tree > not, then lexicographic for determinism.
                tree_required = _collect_course_node_codes(degree_tree)

                def _req_sort(code: str) -> tuple:
                    c = self.courses.get(code)
                    if c is None:
                        return (1, 2, 999, code)
                    in_tree = 0 if code in tree_required else 1
                    has_matching = any(
                        o.campus == campus and o.mode == mode for o in c.offerings
                    )
                    return (in_tree, 0 if has_matching else 1, c.level, code)

                capped: set[str] = set()
                running = 0
                for code in sorted(required_codes, key=_req_sort):
                    if running >= req_budget:
                        break
                    c = self.courses.get(code)
                    if c is None:
                        continue
                    capped.add(code)
                    running += c.credits
                required_codes = capped


        # Step 2: select electives from each pool.
        # Pass the degree credit target so pools with credits=0 (not yet
        # backfilled) can be capped rather than including every pool course.
        #
        # Some pool members also appear as individual COURSE nodes in the
        # requirement tree (a scraping artefact where the same course is
        # listed as both required and elective). Those are always included;
        # only pure-elective pool members (codes that appear only in pools)
        # are subject to the budget cap.
        explicitly_required = _collect_course_node_codes(major_req)
        always_include_in_pool = explicitly_required & pool_codes - required_codes
        pure_elective_pool_codes = pool_codes - explicitly_required

        # Budget for pure-elective selections = degree target minus all
        # definitionally-required credits (required_codes + always_include_in_pool),
        # filtered to courses that will actually be scheduled.
        committed_schedulable = sum(
            self.courses[c].credits
            for c in (required_codes | always_include_in_pool)
            if c in self.courses
            and any(o.campus == campus and o.mode == mode
                    for o in self.courses[c].offerings)
        )
        elective_budget = (
            max(0, degree_total - committed_schedulable)
            if degree_total > 0
            else 0
        )


        # Re-filter the degree tree whenever required courses were capped to
        # prevent validation from demanding courses we intentionally dropped.
        # Two cases:
        #   elective_budget == 0: required alone fill the degree; drop all pools.
        #   required_was_capped and elective_budget > 0: required were trimmed to
        #       make room for pools; drop only the trimmed required COURSE nodes.
        if degree_total > 0 and required_was_capped:
            _cc = {c: self.courses[c].credits for c in self.courses}
            if elective_budget == 0:
                # Required fill the whole degree; drop pool validation nodes too.
                _trim = frozenset(
                    c for c in required_codes
                    if c in self.courses
                    and any(o.campus == campus and o.mode == mode
                            for o in self.courses[c].offerings)
                )
            else:
                # Required were trimmed but pools still contribute; keep pool codes.
                _trim = frozenset(
                    c for c in required_codes | pool_codes
                    if c in self.courses
                    and any(o.campus == campus and o.mode == mode
                            for o in self.courses[c].offerings)
                )
            _rebased = _frt(degree_tree, _trim, _cc)
            if _rebased is not None:
                degree_tree = _rebased

        # Build elective node views that include ONLY pure-elective pool codes.
        # always_include_in_pool codes are handled separately and should not
        # consume the elective budget.
        #
        # When elective_budget == 0, the required courses already fill the degree
        # total. Pool selection is suppressed entirely - adding pools on top would
        # overcredit the plan. The degree tree has already been re-filtered above
        # to remove pool validation nodes in this case.
        pool_credit_scale = 0 if (degree_total > 0 and elective_budget == 0) else 1

        pure_elective_nodes = [
            type(n)(
                credits=n.credits,
                course_codes=tuple(
                    c for c in n.course_codes if c in pure_elective_pool_codes
                ),
            )
            for n in elective_nodes
            if pool_credit_scale > 0 and any(c in pure_elective_pool_codes for c in n.course_codes)
        ]
        elective_codes = self._select_electives(
            pure_elective_nodes, elective_budget, set()
        )
        # Merge always_include_in_pool into elective_codes; they are required
        # but treated as electives by the working set builder.
        elective_codes = elective_codes | always_include_in_pool

        # Step 3: build working set = required + selected electives, minus prior
        working_set = self._build_working_set(
            required_codes, elective_codes, self.prior_completed
        )

        if not working_set:
            campus = self.generator_template.campus
            mode   = self.generator_template.mode
            # Give a specific reason so the CLI can surface a useful message.
            all_codes_count = len(required_codes | elective_codes)
            if all_codes_count == 0:
                raise ValueError(
                    "No courses left to schedule after excluding prior-completed."
                )
            # Build a helpful list of valid campus/mode combos for this major
            valid_combos: list[str] = []
            all_course_codes = required_codes | elective_codes
            combo_counts: dict[tuple[str,str], int] = {}
            for code in all_course_codes:
                c = self.generator_template.courses.get(code)
                if c and c.offerings:
                    for o in c.offerings:
                        key = (o.campus, o.mode)
                        combo_counts[key] = combo_counts.get(key, 0) + 1
            if combo_counts:
                # Sort by how many courses are available in that combo (desc)
                sorted_combos = sorted(combo_counts.items(), key=lambda x: -x[1])
                valid_combos = [f"{cp}/{mo}" for (cp, mo), _ in sorted_combos[:4]]

            hint = (
                f" Valid combinations for this major: {', '.join(valid_combos)}."
                if valid_combos
                else " Try --campus D --mode DIS for distance, or --campus M --mode INT for Manawatū."
            )
            raise ValueError(
                f"No courses left to schedule: all {all_codes_count} major course(s) lack "
                f"a {campus}/{mode} offering.{hint}"
            )

        # Step 4: check prerequisite feasibility before running the scheduler
        if not self._prereqs_feasible(working_set):
            raise ValueError(
                "Selected courses contain an unsatisfiable prerequisite chain."
            )

        # Step 5: run scheduler
        working_credits = sum(c.credits for c in working_set.values())
        # Effective per-semester credit capacity: the lesser of the credit cap
        # and what max_courses_per_semester can deliver. When max_courses=2 and
        # all courses are 15cr, each semester holds at most 30cr - not 60cr.
        _max_courses = self.generator_template.max_courses
        if _max_courses is not None:
            _min_cr = min((c.credits for c in working_set.values() if c.credits > 0), default=15)
            _effective_cap = min(self.generator_template.max_credits, _max_courses * _min_cr)
        else:
            _effective_cap = self.generator_template.max_credits
        min_sems = math.ceil(working_credits / max(1, _effective_cap))
        # Safety multiplier: allow 2.5× the theoretical minimum, but at least 16
        # semesters (8 years) and at most 32 (16 years - an absolute ceiling).
        # The old flat 24 was too small for some postgrad programmes (e.g. 480cr PhDs)
        # and too large a safety net for short diplomas (e.g. 120cr PGCert).
        dynamic_max_semesters = max(16, min(32, math.ceil(min_sems * 2.5)))

        generator = PlanGenerator(
            working_set,
            max_credits_per_semester=self.generator_template.max_credits,
            max_courses_per_semester=self.generator_template.max_courses,
            campus=self.generator_template.campus,
            mode=self.generator_template.mode,
            start_year=self.generator_template.start_year,
            start_semester=self.generator_template.start_semester,
            prior_completed=self.prior_completed,
            max_semesters=dynamic_max_semesters,
            no_summer=self.generator_template.no_summer,
        )
        plan = generator.generate()

        # Step 6: attach prior-completed course objects using the full catalogue
        if self.prior_completed:
            prior_objects = tuple(
                self.courses[code]
                for code in self.prior_completed
                if code in self.courses
            )
            # Reconstruct the plan so __post_init__ recomputes all_course_codes
            # with the prior_completed codes included. Mutating prior_completed
            # directly would leave all_course_codes stale (it is now computed
            # eagerly at construction time, not via cached_property).
            plan = type(plan)(
                semesters=plan.semesters,
                prior_completed=prior_objects,
                transfer_credits=plan.transfer_credits,
            )

        # Step 7: validate - skip only the specific COURSE nodes for codes the
        # student deliberately excluded. The user was warned before generation;
        # we still show the plan but note the unsatisfied requirements.
        validator = DegreeValidator(degree_tree)
        result = validator.validate(plan)
        if not result.passed:
            # If every validation error is a "Missing required course X" where
            # X was explicitly excluded by the student, downgrade to a warning
            # instead of failing entirely. This lets the plan be shown.
            excluded_missing = [
                e for e in result.errors
                if e.startswith("Missing required course ")
                and e.split()[-1].rstrip(".") in self.excluded_courses
            ]
            non_excl_errors = [e for e in result.errors if e not in excluded_missing]
            if non_excl_errors:
                raise ValueError(
                    f"Plan for '{name}' failed degree validation: {'; '.join(non_excl_errors)}"
                )
            # All errors are from student-excluded required courses - log and continue.
            logger.debug(
                "Plan for '%s' is missing %d excluded required course(s): %s",
                name,
                len(excluded_missing),
                ", ".join(e.split()[-1].rstrip(".") for e in excluded_missing),
            )

        # Trim plan to degree_total credits.
        # The working-set prereq expansion may add more courses than needed.
        # Remove lowest-priority courses to bring total to degree_total.
        if degree_total > 0:
            plan_total = plan.total_credits() + plan.prior_credits() + plan.transfer_credits
            if plan_total > degree_total:
                excess = plan_total - degree_total
                # Identify prereqs needed by other plan courses
                plan_codes = {c.code for s in plan.semesters for c in s.courses}
                prereqs_needed: set[str] = set()
                from coursemap.domain.prerequisite import (
                    CoursePrerequisite as _CP3,
                    AndExpression as _AND3,
                    OrExpression as _OR3,
                )
                for s in plan.semesters:
                    for c in s.courses:
                        if c.prerequisites is None:
                            continue
                        stack = [c.prerequisites]
                        while stack:
                            n = stack.pop()
                            if isinstance(n, _CP3) and n.code in plan_codes:
                                prereqs_needed.add(n.code)
                            elif isinstance(n, (_AND3, _OR3)):
                                stack.extend(n.children)
                # Remove non-required, non-preferred, non-tree courses first
                # Protect required, always-include, and direct prereqs of non-elective courses.
                # Pool courses (elective_codes) are optionally removable if they're
                # not in always_include_in_pool, since the pool offers alternatives.
                non_elective_needed: set[str] = set()
                from coursemap.domain.prerequisite import (
                    CoursePrerequisite as _CPt,
                    AndExpression as _ANDt,
                    OrExpression as _ORt,
                )
                for s in plan.semesters:
                    for c in s.courses:
                        if c.code in elective_codes and c.code not in always_include_in_pool:
                            continue  # pool courses don't protect their own prereqs in trim
                        if c.prerequisites is None:
                            continue
                        stk = [c.prerequisites]
                        while stk:
                            n = stk.pop()
                            if isinstance(n, _CPt) and n.code in plan_codes:
                                non_elective_needed.add(n.code)
                            elif isinstance(n, (_ANDt, _ORt)):
                                stk.extend(n.children)

                protected_trim = (required_codes | always_include_in_pool | non_elective_needed)
                # Pool courses are removable (from end of pool, higher-level first)
                # Non-pool extras that are not protected are also removable
                removable_pool = sorted(
                    [c for s in plan.semesters for c in s.courses
                     if c.code in elective_codes
                     and c.code not in protected_trim
                     and c.code not in self.preferred_electives],
                    key=lambda c: (-c.level, c.code),
                )
                removable_extras = sorted(
                    [c for s in plan.semesters for c in s.courses
                     if c.code not in protected_trim
                     and c.code not in elective_codes
                     and c.code not in self.preferred_electives],
                    key=lambda c: (-c.level, c.code),
                )
                removed: set[str] = set()
                for candidate in removable_extras + removable_pool:
                    if excess <= 0:
                        break
                    if candidate.code not in removed:
                        removed.add(candidate.code)
                        excess -= candidate.credits
                if removed:
                    from coursemap.domain.plan import SemesterPlan
                    new_sems = []
                    for sem in plan.semesters:
                        new_courses = [c for c in sem.courses if c.code not in removed]
                        if new_courses:
                            new_sems.append(SemesterPlan(
                                year=sem.year,
                                semester=sem.semester,
                                courses=tuple(new_courses),
                            ))
                    plan = type(plan)(
                        semesters=tuple(new_sems),
                        prior_completed=plan.prior_completed,
                        transfer_credits=plan.transfer_credits,
                    )

        logger.info(
            "Major '%s': %d semesters, %d credits",
            name, len(plan.semesters), plan.total_credits() + plan.prior_credits(),
        )
        return plan, generator

    # ------------------------------------------------------------------
    # Elective selection (greedy)
    # ------------------------------------------------------------------

    def _select_electives(
        self,
        elective_nodes: list[ChooseCreditsRequirement],
        elective_budget: int = 0,
        always_include: set[str] | None = None,
    ) -> set[str]:
        """
        Choose courses from elective pools using a greedy, delivery-mode-aware strategy.

        Selection prioritises courses that are schedulable in the configured campus/mode.
        Credit targets are satisfied using schedulable credits only - non-schedulable
        courses will be dropped by _build_working_set, so counting them toward a credit
        target produces plans that fail validation. Non-schedulable courses are still
        included as a fallback when a pool cannot be satisfied by schedulable courses
        alone, but they are never counted toward the credit target.

        always_include: codes that appear as COURSE nodes AND pool members; always
        scheduled regardless of budget since the validation tree requires them directly.

        For pools with credits > 0: select the minimum schedulable courses meeting the
        credit target, preferring always_include codes then elective_sort_key order.
        Each pool's effective target is further capped by the remaining elective_budget
        so that when required courses already consume the full degree total, no pool
        courses are added (prevents overcrediting from scraped data that over-captures
        required courses for a given degree).

        For pools with credits == 0: use elective_budget to cap selection across pools.
        When elective_budget == 0, include all pool courses (no credit target known).

        Top-up: when all pools have credits > 0 but their targets sum to less than
        elective_budget, continue selecting from pools to meet the degree total.
        """
        if always_include is None:
            always_include = set()

        campus = self.generator_template.campus
        mode   = self.generator_template.mode

        def is_schedulable(code: str) -> bool:
            if code in self.excluded_courses:
                return False
            c = self.courses.get(code)
            return c is not None and any(
                o.campus == campus and o.mode == mode for o in c.offerings
            )

        selected: set[str] = set()
        zero_credit_nodes = [n for n in elective_nodes if n.credits <= 0]
        known_credit_nodes = [n for n in elective_nodes if n.credits > 0]

        # Pre-load always_include codes (required by COURSE nodes too).
        # They count toward the budget only if schedulable.
        always_credits = sum(
            self.courses[c].credits for c in always_include
            if c in self.courses and is_schedulable(c)
        )
        if elective_budget <= 0 or always_credits <= elective_budget:
            selected.update(always_include)
            force_include: set[str] = set()
        else:
            force_include = set(always_include)

        def _sort_key(code: str) -> tuple:
            """Force-include first, schedulable second, then elective_sort_key order."""
            return (
                0 if code in force_include else 1,
                0 if is_schedulable(code) else 1,
            ) + self._elective_sort_key(code)

        # ----------------------------------------------------------------
        # Pass 1: pools with known credit targets.
        # Count only schedulable credits toward the target; non-schedulable
        # courses are only added as a last resort when the schedulable subset
        # is insufficient.
        #
        # Each pool's effective credit target is capped by the remaining
        # elective_budget so that when required courses already consume the
        # full degree total, no additional pool courses are selected.
        # ----------------------------------------------------------------

        def _sched_credits(codes: Iterable[str]) -> int:
            return sum(
                self.courses[c].credits for c in codes
                if c in self.courses and is_schedulable(c)
            )

        # Schedulable credits already committed (always_include pre-loaded above).
        committed_sched = _sched_credits(selected)

        for node in known_credit_nodes:
            # Effective target = pool's own credit requirement.
            effective_target = node.credits

            # Credits already in `selected` from this pool (schedulable only).
            accumulated_sched = _sched_credits(
                c for c in node.course_codes if c in selected
            )
            if accumulated_sched >= effective_target:
                continue

            # Part I/II pair detection: before greedy sweep, check whether any
            # pair of courses that are explicitly "Part I" + "Part II" of the same
            # title meets the credit target. If so, prefer that pair - it avoids
            # mixing parts from different thesis tracks (e.g. taking the 90cr thesis
            # Part I alongside the 120cr thesis Part I, which is nonsensical).
            #
            # Approach: build (part1, part2) groups from title matching, compute
            # combined schedulable credits, pick the group with minimum overshoot
            # that meets the target. If no pair group qualifies, fall through to
            # the standard greedy loop.
            remaining_needed = effective_target - accumulated_sched
            best_group: list[str] | None = None
            best_overshoot = float("inf")

            _part1_re = re.compile(r"\bPart\s*I\b(?!I)", re.IGNORECASE)
            _part2_re = re.compile(r"\bPart\s*II\b", re.IGNORECASE)

            unselected_sched = [
                c for c in node.course_codes
                if c not in selected and c in self.courses and is_schedulable(c)
            ]
            part1_candidates = [c for c in unselected_sched
                                 if _part1_re.search(self.courses[c].title)]
            for p1 in part1_candidates:
                cr1 = self.courses[p1].credits
                # Find a Part II in the same pool with the same credit value.
                p2_match = next(
                    (c for c in unselected_sched
                     if c != p1
                     and self.courses[c].credits == cr1
                     and _part2_re.search(self.courses[c].title)),
                    None,
                )
                if p2_match is None:
                    continue
                pair_credits = cr1 * 2
                if pair_credits >= remaining_needed and remaining_needed > 0:
                    overshoot = pair_credits - remaining_needed
                    if overshoot < best_overshoot:
                        best_overshoot = overshoot
                        best_group = [p1, p2_match]

            if best_group is not None:
                for code in best_group:
                    selected.add(code)
                    if is_schedulable(code):
                        accumulated_sched += self.courses[code].credits
            else:
                sorted_codes = sorted(node.course_codes, key=_sort_key)
                for code in sorted_codes:
                    if accumulated_sched >= effective_target:
                        break
                    if code in selected:
                        continue
                    course = self.courses.get(code)
                    if course is None:
                        continue
                    selected.add(code)
                    if is_schedulable(code):
                        accumulated_sched += course.credits

            if accumulated_sched < effective_target and effective_target > 0:
                # Not enough schedulable courses - include everything and let
                # filter_requirement_tree cap the validation target.
                logger.debug(
                    "Elective pool needs %dcr (effective %dcr) but only %dcr schedulable "
                    "in %s/%s; including all pool courses.",
                    node.credits, effective_target, accumulated_sched, campus, mode,
                )
                selected.update(
                    c for c in node.course_codes if c in self.courses
                )

            # Update committed_sched so the next pool sees the correct remaining budget.
            committed_sched = _sched_credits(selected)

        # ----------------------------------------------------------------
        # Top-up pass: when all pools have credits > 0 but their targets
        # sum to less than elective_budget (e.g. a 120cr GradDip with a
        # single 60cr pool), keep selecting to reach the degree total.
        #
        # When pool_target_sum > elective_budget the pool has been
        # over-captured (scraped more courses than the degree needs).
        # In that case cap at pool_target_sum to prevent runaway selection.
        # Otherwise use elective_budget as usual.
        # ----------------------------------------------------------------
        if not zero_credit_nodes and elective_budget > 0:
            pool_target_sum = sum(n.credits for n in known_credit_nodes)
            sched_selected = sum(
                self.courses[c].credits
                for c in selected
                if c in self.courses and is_schedulable(c)
            )
            # Only cap at pool_target_sum when the pools over-specify (more
            # pool courses exist than the degree target allows).
            if pool_target_sum > elective_budget:
                topup_cap = pool_target_sum
            else:
                topup_cap = elective_budget
            remaining = topup_cap - sched_selected
            if remaining > 0:
                for node in known_credit_nodes:
                    for code in sorted(node.course_codes, key=_sort_key):
                        if remaining <= 0:
                            break
                        if code in selected:
                            continue
                        course = self.courses.get(code)
                        if course is None or not is_schedulable(code):
                            continue
                        selected.add(code)
                        remaining -= course.credits
                    if remaining <= 0:
                        break

        if not zero_credit_nodes:
            return selected

        # ----------------------------------------------------------------
        # Pass 2: pools with credits == 0 (not yet backfilled).
        # Use elective_budget to cap; distribute across pools greedily.
        # ----------------------------------------------------------------
        committed_sched = sum(
            self.courses[c].credits
            for c in selected
            if c in self.courses and is_schedulable(c)
        )
        remaining_budget = max(0, elective_budget - committed_sched) if elective_budget > 0 else 0

        if remaining_budget > 0 and zero_credit_nodes:
            schedulable_per_pool: list[list[str]] = []
            for node in zero_credit_nodes:
                sched = sorted(
                    (
                        c for c in node.course_codes
                        if c in self.courses and is_schedulable(c)
                    ),
                    key=lambda c: (0 if c in force_include else 1,) + self._elective_sort_key(c),
                )
                schedulable_per_pool.append(sched)

            budget_used = 0
            pool_idx: list[int] = [0] * len(zero_credit_nodes)
            changed = True
            while changed and budget_used < remaining_budget:
                changed = False
                for pi, schedulable in enumerate(schedulable_per_pool):
                    while pool_idx[pi] < len(schedulable):
                        code = schedulable[pool_idx[pi]]
                        pool_idx[pi] += 1
                        if code in selected:
                            continue
                        cr = self.courses[code].credits
                        if budget_used + cr > remaining_budget:
                            continue
                        selected.add(code)
                        budget_used += cr
                        changed = True
                        break

            # Overshoot: close any remaining gap with the smallest schedulable course.
            if budget_used < remaining_budget:
                candidates = [
                    (self.courses[c].credits, c)
                    for pool in schedulable_per_pool
                    for c in pool
                    if c not in selected and c in self.courses
                ]
                if candidates:
                    candidates.sort()
                    selected.add(candidates[0][1])

        elif elective_budget == 0:
            for node in zero_credit_nodes:
                selected.update(c for c in node.course_codes if c in self.courses)

        return selected

    def _elective_sort_key(self, code: str) -> tuple:
        """
        Sort key for greedy elective selection. Lower = more preferred.

        Priority (ascending, so lowest wins):
          0. Not preferred (1) vs preferred (0)  -- student preferences first
          1. Earliest semester offered (S1=0, S2=1, SS=2, none=3)
          2. Course level (100 before 200 before 300)
          3. Course code (alphabetical, for determinism)
        """
        preferred = 0 if code in self.preferred_electives else 1

        course = self.courses.get(code)
        if course is None:
            return (preferred, 3, 999, code)

        sem_order = {"S1": 0, "S2": 1, "SS": 2}
        earliest_sem = min(
            (sem_order.get(o.semester, 3) for o in course.offerings),
            default=3,
        )
        return (preferred, earliest_sem, course.level, code)

    # ------------------------------------------------------------------
    # Course subset construction
    # ------------------------------------------------------------------

    def _build_working_set(
        self,
        major_codes: set[str],
        elective_codes: set[str],
        prior_completed: frozenset[str],
    ) -> dict[str, Course]:
        """
        Build the set of courses the scheduler should actually plan.

        Automatically expands the set to include any prerequisite-chain
        courses needed to satisfy prerequisites of major/elective courses
        that aren't already in the set.  For example, if a major requires
        159201 (which needs 159102, which needs 159101), all three are
        included so the scheduler respects the correct ordering.

        Excludes:
        - Courses the student has already completed.
        - Courses the student has explicitly excluded (--exclude flag).
        - Courses with no offering matching the configured campus and delivery
          mode. An internal-only course in a distance plan would deadlock the
          scheduler (it passes prerequisite checks but is never offered).
        - When no_summer=True, courses only available in Summer School (SS).
          These are permanently unschedulable in no-summer plans and would
          cause the generator to loop until the horizon is exceeded.
        """
        campus    = self.generator_template.campus
        mode      = self.generator_template.mode
        no_summer = self.generator_template.no_summer
        all_codes = set(major_codes) | set(elective_codes)

        def _has_valid_offering(course: Course) -> bool:
            matching = [o for o in course.offerings if o.campus == campus and o.mode == mode]
            if not matching:
                return False
            if no_summer:
                return any(o.semester in ("S1", "S2") for o in matching)
            return True

        # --- Expand prerequisite chains -----------------------------------
        # Walk the prereq graph of every course in all_codes and add any
        # prerequisite course that:
        #   a) exists in the full course catalogue
        #   b) has not already been completed by the student
        #   c) has a valid offering for this campus/mode
        # This ensures that e.g. 159201 (requires 159102 → 159101 → 159100)
        # causes all four courses to be included in the working set, so the
        # scheduler enforces the correct semester ordering.
        def _collect_prereq_codes(code: str, visited: set[str]) -> set[str]:
            if code in visited or code not in self.courses:
                return set()
            visited.add(code)
            course = self.courses[code]
            prereq = course.prerequisites
            if prereq is None:
                return set()
            needed: set[str] = set()
            stack = [prereq]
            while stack:
                node = stack.pop()
                from coursemap.domain.prerequisite import (
                    CoursePrerequisite, AndExpression, OrExpression,
                )
                if isinstance(node, CoursePrerequisite):
                    child = node.code
                    if (
                        child not in visited
                        and child in self.courses
                        and child not in prior_completed
                        and child not in self.excluded_courses
                    ):
                        needed.add(child)
                        needed.update(_collect_prereq_codes(child, visited))
                elif isinstance(node, (AndExpression, OrExpression)):
                    stack.extend(node.children)
            return needed

        # Expand prereq chains. Pass only the expansion set as "visited"
        # (not all_codes) so we can collect prereqs for major courses.
        # Codes already in all_codes are the destination; we want to find
        # the courses needed *before* them.
        expansion: set[str] = set()
        for code in list(all_codes):
            expansion.update(_collect_prereq_codes(code, set(expansion)))
        # Remove codes that are already in the original set to avoid duplication
        expansion -= all_codes
        all_codes.update(expansion)
        # ------------------------------------------------------------------

        return {
            code: self.courses[code]
            for code in all_codes
            if (
                code in self.courses
                and code not in prior_completed
                and code not in self.excluded_courses
                and _has_valid_offering(self.courses[code])
            )
        }

    # ------------------------------------------------------------------
    # Prerequisite feasibility check
    # ------------------------------------------------------------------

    def _prereqs_feasible(self, working_set: dict[str, Course]) -> bool:
        """
        Check that the working set can be ordered without a deadlock.

        Simulates the scheduler's topological pass: repeatedly marks courses
        as completable once their prerequisites are met. Returns False if
        progress stalls before all schedulable courses are processed.

        Courses with no offerings are skipped: they will never be eligible
        in the scheduler either, but they should not block the feasibility
        check for courses that do have offerings.

        Prerequisites outside the working set (and not in prior_completed)
        are treated as pre-satisfied -- they are admission gatekeepers or
        out-of-scope courses.
        """
        known = set(working_set.keys()) | self.prior_completed
        # Only consider courses that actually have offerings
        schedulable = {
            code for code, c in working_set.items() if c.offerings
        }
        remaining = set(schedulable)
        completed: set[str] = set(self.prior_completed)

        while remaining:
            unlocked = [
                code for code in remaining
                if prereqs_met(working_set[code].prerequisites, completed, known)
            ]
            if not unlocked:
                return False
            for code in unlocked:
                remaining.discard(code)
                completed.add(code)

        return True
