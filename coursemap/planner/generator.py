"""
Greedy topological scheduler for degree planning.

PlanGenerator takes a working set of courses (already filtered to the
student's campus and delivery mode) and places them into semesters in
the earliest valid order. A course is eligible in a given semester when
all its prerequisites have been placed in prior semesters and it is
offered in that semester type.

The algorithm is O(n * s) where n is the number of courses and s is the
number of semester slots generated. A rebalancing post-pass pulls eligible
courses forward from earlier semesters when the final semester is underfilled.

This approach is intentionally simple. Backtracking over elective combinations
or full constraint propagation would handle multi-pool interaction edge cases
but at complexity cost not warranted by the dataset size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coursemap.domain.course import Course
from coursemap.domain.plan import DegreePlan, SemesterPlan
from coursemap.domain.prerequisite_utils import prereqs_met

logger = logging.getLogger(__name__)

REBALANCE_THRESHOLD = 30  # credits below which the final semester triggers rebalancing
# For postgrad plans where courses are 30cr, use a higher threshold
_REBALANCE_THRESHOLD_POSTGRAD = 60


@dataclass
class PlanStats:
    """Scheduling statistics collected during a single generate() call."""
    courses_scheduled: int = 0
    prerequisite_rejections: int = 0  # per-semester course skips due to unmet prereqs
    offering_rejections: int = 0      # per-semester course skips due to no matching offering
    semesters_generated: int = 0      # semester slots that contained at least one course
    empty_semesters_skipped: int = 0  # semester slots where nothing was eligible
    rebalance_moves: int = 0          # courses repositioned by post-pass rebalancing


class PlanGenerator:
    """
    Places courses into semesters using a greedy topological sort.

    Each semester slot, eligible courses (prerequisites met, offered this
    semester) are filled in code order up to the credit cap. Corequisites
    are expanded atomically so co-scheduled pairs are never split.

    Offerings repeat annually, so only the semester type (S1/S2/SS) matters,
    not the calendar year.
    """

    def __init__(
        self,
        courses: dict[str, Course],
        max_credits_per_semester: int = 60,
        max_courses_per_semester: int | None = None,
        campus: str = "D",
        mode: str = "DIS",
        start_year: int = 2026,
        start_semester: str = "S1",
        max_semesters: int = 24,
        prior_completed: frozenset = frozenset(),
        no_summer: bool = False,
    ):
        self.courses = courses
        self.max_credits = max_credits_per_semester
        self.max_courses = max_courses_per_semester
        self.campus = campus
        self.mode = mode
        self.start_year = start_year
        self.start_semester = start_semester.upper()
        self.max_semesters = max_semesters
        self.prior_completed: frozenset = prior_completed
        self.no_summer = no_summer
        self._known_codes: set[str] = set(self.courses)
        self.stats: PlanStats = PlanStats()

    def generate(self) -> DegreePlan:
        self.stats = PlanStats()

        # Pre-completed courses are excluded from scheduling but seed the
        # completed set so their codes satisfy prerequisite checks.
        # Zero-credit courses (practicums, language enrollments) are non-schedulable
        # and must never enter the remaining set - they would deadlock the scheduler.
        remaining: set[str] = {
            code for code in self.courses.keys()
            if code not in self.prior_completed
            and self.courses[code].credits > 0
        }
        completed: set[str] = set(self.prior_completed)
        semesters: list[SemesterPlan] = []

        base_year = self.start_year
        semester_cycle = ["S1", "S2", "SS"]
        # Start at the requested semester within the cycle
        _sem_start = {"S1": 0, "S2": 1, "SS": 2}
        semester_index = _sem_start.get(self.start_semester, 0)
        # Adjust base_year so year arithmetic is correct when starting mid-cycle
        # Each full cycle = 1 year; starting at index 1 or 2 means we're already
        # partway into the year, so no year adjustment needed - the year increments
        # naturally when index // 3 increases.

        # Count only *active* semesters (not skipped SS slots) against the
        # safe-horizon limit. Previously, semester_index counted every cycle
        # slot including no_summer-skipped ones, which cut the real budget to
        # max_semesters * 2/3 when no_summer=True. Now we track active slots
        # separately so max_semesters always means max_semesters of real teaching.
        active_semesters = 0

        while remaining:
            if active_semesters >= self.max_semesters:
                raise ValueError(
                    "Scheduling exceeded safe horizon. "
                    "Possible unsatisfiable prerequisites or offerings."
                )

            semester_name = semester_cycle[semester_index % 3]
            current_year = base_year + (semester_index // 3)
            semester_index += 1

            # Skip Summer School semesters when --no-summer is set.
            # We still advance the index to keep the S1/S2/SS cycle in sync.
            if self.no_summer and semester_name == "SS":
                self.stats.empty_semesters_skipped += 1
                continue

            # Count only real (non-SS-skipped) semesters against the horizon.
            active_semesters += 1

            eligible, offering_rej, prereq_rej = self._eligible_courses(
                remaining, completed, semester_name
            )
            self.stats.offering_rejections += offering_rej
            self.stats.prerequisite_rejections += prereq_rej

            if not eligible:
                if not self._any_future_possible(remaining, completed):
                    raise ValueError(
                        "No schedulable courses remain. "
                        "Prerequisite or offering deadlock detected."
                    )
                self.stats.empty_semesters_skipped += 1
                logger.debug(
                    "Semester %s %s: nothing eligible (%d offering, %d prereq rejections)",
                    current_year, semester_name, offering_rej, prereq_rej,
                )
                continue

            semester_courses = []
            credits = 0
            already_added: set[str] = set()

            eligible_set: set[str] = set(eligible)

            for code in sorted(eligible):
                if code in already_added:
                    continue

                group = self._expand_coreq_group(code, eligible_set, completed)
                if group is None:
                    # A required corequisite is not available this semester
                    continue

                group_credits = sum(self.courses[c].credits for c in group)

                # A course group that exceeds the per-semester cap (e.g. a 90cr
                # dissertation) must be scheduled alone. Skip it only when the
                # semester already has other courses and adding this group would
                # breach the cap.
                if credits > 0 and credits + group_credits > self.max_credits:
                    continue

                # Course-count cap: --max-per-semester N
                if (
                    self.max_courses is not None
                    and len(already_added) + len(group) > self.max_courses
                ):
                    continue

                for c in sorted(group):
                    semester_courses.append(self.courses[c])
                    credits += self.courses[c].credits
                    already_added.add(c)

            if not semester_courses:
                self.stats.empty_semesters_skipped += 1
                continue

            for course in semester_courses:
                remaining.remove(course.code)
                completed.add(course.code)

            self.stats.courses_scheduled += len(semester_courses)
            self.stats.semesters_generated += 1

            logger.debug(
                "Semester %s %s: scheduled %d course(s), %d credits",
                current_year, semester_name, len(semester_courses), credits,
            )

            semesters.append(
                SemesterPlan(
                    year=current_year,
                    semester=semester_name,
                    courses=tuple(semester_courses),
                )
            )

        semesters = self._rebalance(semesters)

        # Update semesters_generated to reflect post-rebalance count (Pass 3 may
        # have merged two semesters into one, reducing the count by one).
        self.stats.semesters_generated = len(semesters)

        logger.debug(
            "Generation complete: %d courses in %d semesters "
            "(%d prereq rejections, %d offering rejections, "
            "%d empty slots skipped, %d rebalance moves)",
            self.stats.courses_scheduled,
            self.stats.semesters_generated,
            self.stats.prerequisite_rejections,
            self.stats.offering_rejections,
            self.stats.empty_semesters_skipped,
            self.stats.rebalance_moves,
        )

        # prior_completed course objects are attached by the caller (PlanSearch),
        # which holds the full course catalogue. The generator only receives the
        # selected subset and cannot look up codes that were deliberately excluded.
        return DegreePlan(tuple(semesters))

    def _rebalance(self, semesters: list[SemesterPlan]) -> list[SemesterPlan]:
        """
        Post-pass rebalancing: when the final semester is underfilled (< threshold
        credits), attempt to move flexible courses from earlier semesters into it.

        The rebalance threshold is now adaptive: it's set to 1.5× the median
        course credit value in the plan, clamped to [15, 60]. This means a
        postgrad plan of 30cr courses uses threshold=45, not 30 - correctly
        triggering rebalancing. An undergrad plan of 15cr courses uses threshold=22.

        A course is a valid rebalancing candidate when:
          (a) it is offered in the same semester type as the final (flexibility check),
          (b) no course scheduled in a semester strictly between the source and the final
              lists it as a prerequisite (safety check),
          (c) moving it would not push the final semester over max_credits (capacity),
          (d) removing it would leave the source semester non-empty.

        After pulling candidates into the final, any source semester that is now empty
        is dropped (Pass 2).  Finally, if the final and penultimate semesters share the
        same semester name and their combined credits fit within max_credits, they are
        merged into one (Pass 3).

        The method is a no-op when the final semester already meets the threshold or
        when fewer than two semesters exist.  It never raises: if rebalancing is
        impossible the original list is returned unchanged.
        """
        if len(semesters) < 2:
            return semesters

        # Compute adaptive threshold based on median course credit in this plan.
        all_credits = [c.credits for s in semesters for c in s.courses if c.credits > 0]
        if all_credits:
            sorted_cr = sorted(all_credits)
            median_cr = sorted_cr[len(sorted_cr) // 2]
            threshold = max(15, min(60, int(median_cr * 1.5)))
        else:
            threshold = REBALANCE_THRESHOLD

        final = semesters[-1]
        if final.total_credits() >= threshold:
            return semesters

        logger.debug(
            "Rebalancing: final semester %s %s has only %d credits (adaptive threshold %d)",
            final.year, final.semester, final.total_credits(), threshold,
        )

        # Work with mutable lists of course tuples for easy manipulation.
        sem_courses: list[list[Course]] = [list(s.courses) for s in semesters]

        # Pass 1: pull flexible courses from earlier semesters into the final.
        #
        # Walk backwards through all prior semesters.  For each, collect the
        # prerequisite codes required by every course that sits between this
        # source semester and the final; those courses must not be moved.
        for src_idx in range(len(sem_courses) - 2, -1, -1):
            if sem_courses[-1] and sum(c.credits for c in sem_courses[-1]) >= threshold:
                break  # already fixed

            # Build the set of codes needed as prerequisites by courses in
            # intermediate semesters (src_idx+1 .. n-2 inclusive).
            needed_by_intermediate: set[str] = set()
            for mid_idx in range(src_idx + 1, len(sem_courses) - 1):
                for course in sem_courses[mid_idx]:
                    if course.prerequisites:
                        needed_by_intermediate |= (
                            course.prerequisites.required_courses() & self._known_codes
                        )

            final_credits = sum(c.credits for c in sem_courses[-1])
            final_sem_name = semesters[-1].semester

            # Prereq codes required by courses already in the final semester.
            # Recompute per source-semester iteration because previous moves
            # may have added courses (with their own prereqs) to the final.
            needed_by_final: set[str] = set()
            for fc in sem_courses[-1]:
                if fc.prerequisites:
                    needed_by_final |= (
                        fc.prerequisites.required_courses() & self._known_codes
                    )

            # Iterate over a snapshot so we can remove from the live list.
            for course in list(sem_courses[src_idx]):
                if final_credits >= threshold:
                    break

                # (a) flexibility: offered in the final semester type
                if not course.is_offered(final_sem_name, self.campus, self.mode):
                    continue

                # (b) safety: not a prerequisite for any intermediate course
                if course.code in needed_by_intermediate:
                    continue

                # (c) capacity: fits in the final
                if final_credits + course.credits > self.max_credits:
                    continue

                # (d) non-empty source: at least one course remains after removal
                if len(sem_courses[src_idx]) <= 1:
                    continue

                # (e) safety: not a prerequisite for any course already in the final.
                #     Moving a course into the final alongside a course that needs it
                #     as a prerequisite would violate ordering.
                if course.code in needed_by_final:
                    continue

                # (f) corequisite safety: do not move a course that has unmet
                #     corequisites.  Co-scheduling constraints cannot be safely
                #     re-evaluated after the greedy pass, so such courses are left
                #     in their original semester.
                if course.corequisites & self._known_codes:
                    continue

                # Move the course.
                sem_courses[src_idx].remove(course)
                sem_courses[-1].append(course)
                final_credits += course.credits
                self.stats.rebalance_moves += 1

                # Update needed_by_final: the newly added course may itself have prereqs
                # that block subsequent candidates from this same source semester.
                if course.prerequisites:
                    needed_by_final |= (
                        course.prerequisites.required_courses() & self._known_codes
                    )

                logger.debug(
                    "Rebalance: moved %s (%s) from semester %d into final",
                    course.code, course.title, src_idx,
                )

        # Track which original semester indices are still live (non-empty after Pass 1).
        # sem_courses[i] was originally semesters[i], and courses may have been moved
        # out of some slots.  We need to know which original index each live slot maps to
        # so we can assign the correct year/semester label.
        surviving_indices = [i for i, cl in enumerate(sem_courses) if cl]

        # Pass 2: drop empty slots from sem_courses.
        sem_courses = [cl for cl in sem_courses if cl]

        # Rebuild SemesterPlan objects using the original label for each surviving slot.
        result: list[SemesterPlan] = [
            SemesterPlan(
                year=semesters[orig_i].year,
                semester=semesters[orig_i].semester,
                courses=tuple(courses_list),
            )
            for orig_i, courses_list in zip(surviving_indices, sem_courses)
        ]

        if len(result) < 2:
            return result

        # Pass 3: if the final and penultimate share the same semester type and
        # their combined load fits within max_credits, merge them.
        #
        # Guard: do not merge when any course in the final requires a course in
        # the penultimate as a prerequisite.  The greedy pass scheduled them in
        # separate semesters for a reason - merging would produce a plan where
        # a course is co-scheduled with its own prerequisite.
        penultimate = result[-2]
        final = result[-1]
        combined = penultimate.total_credits() + final.total_credits()
        pen_codes = {c.code for c in penultimate.courses}
        fin_needs_pen = any(
            c.prerequisites
            and (c.prerequisites.required_courses() & self._known_codes) & pen_codes
            for c in final.courses
        )
        combined_count = len(penultimate.courses) + len(final.courses)
        if (
            penultimate.semester == final.semester
            and combined <= self.max_credits
            and not fin_needs_pen
            and (self.max_courses is None or combined_count <= self.max_courses)
        ):
            merged = SemesterPlan(
                year=penultimate.year,
                semester=penultimate.semester,
                courses=tuple(list(penultimate.courses) + list(final.courses)),
            )
            result = result[:-2] + [merged]
            logger.debug(
                "Rebalance: merged final into penultimate → %s %s (%d credits)",
                merged.year, merged.semester, merged.total_credits(),
            )

        if self.stats.rebalance_moves:
            logger.debug(
                "Rebalance complete: %d move(s), %d semester(s) final",
                self.stats.rebalance_moves, len(result),
            )

        # Pass 4: mid-plan equalisation.
        # Pull courses from overfull semesters forward into underfull preceding
        # semesters of the same type (e.g. S2→S2, S1→S1).  This smooths the
        # load staircases that the greedy pass creates when prerequisites unlock
        # a large batch all at once.
        #
        # IMPORTANT: this pass only operates on semesters [0..n-2].  The final
        # semester was already tuned by Pass 1-3 above; touching it here would
        # undo that work.
        equalise_moves = self._equalise(result)
        if equalise_moves:
            logger.debug("Equalise: %d move(s) across mid-plan semesters", equalise_moves)

        return result

    def _equalise(self, semesters: list[SemesterPlan]) -> int:
        """
        Pass 4: smooth load imbalances across same-type semesters (excluding final).

        For each dst semester that is underfull, look ahead up to WINDOW semesters
        for a src with the same type that has movable courses.  A course can move
        dst←src when:
          (a) dst.semester == src.semester  (offering guaranteed same type)
          (b) fits within dst's credit cap
          (c) is not needed as a prereq by any intermediate-semester course
          (d) is not a prereq of anything already in dst
          (e) does not itself require anything in dst..src-1 as a prereq
          (f) src stays non-empty after removal
          (g) no active corequisite complications

        Returns the number of moves performed (does NOT modify self.stats so the
        rebalance_moves counter stays accurate for Pass 1-3 expectations).
        """
        n = len(semesters)
        if n < 3:
            # Need at least 3 semesters for a meaningful mid-plan equalisation
            # (Pass 1-3 already handle the 2-semester case).
            return 0

        sem_courses: list[list[Course]] = [list(s.courses) for s in semesters]
        total_moves = 0

        WINDOW = 4  # only look this many semesters ahead per destination

        # Only equalise among non-final semesters: range(n-1).
        for dst_idx in range(n - 1):
            dst_credits = sum(c.credits for c in sem_courses[dst_idx])
            if dst_credits >= self.max_credits:
                continue  # already at cap

            dst_sem = semesters[dst_idx].semester

            for src_idx in range(dst_idx + 1, min(dst_idx + WINDOW + 1, n - 1)):
                # Only operate within non-final semesters (src_idx < n-1).
                if semesters[src_idx].semester != dst_sem:
                    continue

                # Codes needed by courses in intermediate semesters as prereqs.
                needed_between: set[str] = set()
                for mid_idx in range(dst_idx + 1, src_idx):
                    for course in sem_courses[mid_idx]:
                        if course.prerequisites:
                            needed_between |= (
                                course.prerequisites.required_courses() & self._known_codes
                            )

                # Codes that dst's existing courses need as prereqs (block moving
                # a course that dst already depends on arriving later).
                needed_by_dst: set[str] = set()
                for fc in sem_courses[dst_idx]:
                    if fc.prerequisites:
                        needed_by_dst |= (
                            fc.prerequisites.required_courses() & self._known_codes
                        )

                for course in list(sem_courses[src_idx]):
                    dst_credits = sum(c.credits for c in sem_courses[dst_idx])
                    if dst_credits >= self.max_credits:
                        break

                    # (b) credit cap
                    if dst_credits + course.credits > self.max_credits:
                        continue

                    # (c) course-count cap
                    if (
                        self.max_courses is not None
                        and len(sem_courses[dst_idx]) >= self.max_courses
                    ):
                        break

                    # (d) not needed by intermediate courses as prereq
                    if course.code in needed_between:
                        continue

                    # (d2) not needed by dst's own courses as prereq
                    if course.code in needed_by_dst:
                        continue

                    # (e) course must not need anything in dst..src-1 as prereq
                    prereq_codes = (
                        course.prerequisites.required_courses() & self._known_codes
                        if course.prerequisites else set()
                    )
                    blocking: set[str] = set()
                    for mid_idx in range(dst_idx, src_idx):
                        blocking |= {c.code for c in sem_courses[mid_idx]}
                    if prereq_codes & blocking:
                        continue

                    # (f) source stays non-empty
                    if len(sem_courses[src_idx]) <= 1:
                        continue

                    # (g) no corequisite complications
                    if course.corequisites & self._known_codes:
                        continue

                    # Move.
                    sem_courses[src_idx].remove(course)
                    sem_courses[dst_idx].append(course)
                    needed_by_dst |= prereq_codes
                    total_moves += 1

                    logger.debug(
                        "Equalise: moved %s from sem[%d] to sem[%d]",
                        course.code, src_idx, dst_idx,
                    )

        # Rebuild SemesterPlan objects (SemesterPlan is frozen - create new instances).
        # Drop any semesters that became empty (shouldn't happen since we guard
        # source non-empty, but be safe).

        # SemesterPlan is frozen - rebuild the list.
        result: list[SemesterPlan] = []
        for i, cl in enumerate(sem_courses):
            if cl:
                result.append(SemesterPlan(
                    year=semesters[i].year,
                    semester=semesters[i].semester,
                    courses=tuple(cl),
                ))
            # else: equalise left this empty - drop it (shouldn't happen)

        # Replace contents of the input list so caller sees updated semesters.
        semesters[:] = result

        return total_moves

    def _expand_coreq_group(
        self,
        code: str,
        eligible_set: set[str],
        completed: set[str],
    ) -> set[str] | None:
        """
        Expand a course into the full set of courses that must be scheduled
        together this semester due to corequisite constraints.

        A corequisite is satisfied when its code is either:
          - already in completed (taken in a prior semester), or
          - in eligible_set (will be co-enrolled this semester).

        Returns the atomic group (set of codes to schedule together), or None
        if any required corequisite cannot be satisfied this semester.

        When there are no corequisites the group is just {code}.
        Out-of-scope corequisite codes (not in self._known_codes) are treated
        as already satisfied, matching the "unknown = satisfied" convention
        used throughout the prereq system.
        """
        group: set[str] = set()
        queue = [code]

        while queue:
            cur = queue.pop()
            if cur in group:
                continue
            group.add(cur)

            course = self.courses.get(cur)
            if course is None or not course.corequisites:
                continue

            for coreq in course.corequisites:
                if coreq not in self._known_codes:
                    continue  # out-of-scope: treated as satisfied
                if coreq in completed:
                    continue  # already done
                if coreq not in eligible_set:
                    return None  # required coreq not available this semester
                if coreq not in group:
                    queue.append(coreq)

        return group

    def _eligible_courses(
        self,
        remaining: set[str],
        completed: set[str],
        semester: str,
    ) -> tuple[list[str], int, int]:
        """
        Return (eligible_codes, offering_rejections, prereq_rejections).

        A course is eligible when:
          (a) it is offered this semester at the configured campus/mode,
          (b) its prerequisites are met (or are outside the known set),
          (c) none of its restriction codes have been completed.

        Counts are per-semester-pass and accumulate into self.stats in generate().
        Note: corequisite checking is deferred to the greedy fill loop so that
        co-enrolled courses can satisfy each other's corequisites atomically.
        """
        eligible = []
        offering_rej = 0
        prereq_rej = 0

        for code in remaining:
            course = self.courses[code]

            if not course.is_offered(
                semester=semester,
                campus=self.campus,
                mode=self.mode,
            ):
                offering_rej += 1
                continue

            if (
                course.prerequisites
                and not prereqs_met(course.prerequisites, completed, self._known_codes)
            ):
                prereq_rej += 1
                continue

            # Restriction check: if any known restriction code is in completed,
            # this course is permanently blocked for this student.
            if course.restrictions:
                known_restrictions = course.restrictions & self._known_codes
                if known_restrictions & completed:
                    prereq_rej += 1
                    continue

            eligible.append(code)

        return eligible, offering_rej, prereq_rej

    def _any_future_possible(
        self,
        remaining: set[str],
        completed: set[str],
    ) -> bool:
        """
        Detect global deadlock: is there any remaining course that can ever
        be scheduled in the configured campus and delivery mode?

        A course is permanently blocked when:
        - It has no offerings at all (dataset gap).
        - It has no offering matching the configured campus and mode (e.g.
          an internal-only course in a distance plan).
        - When no_summer=True, it is offered only in SS (can never be scheduled).
        - A restriction code has been completed.
        - Its prerequisites are satisfied (used as a proxy for "could be
          scheduled in some future semester").

        Any course not permanently blocked counts as future-possible even if
        its prerequisites are not yet met -- they may be met in a later semester.
        """
        for code in remaining:
            course = self.courses[code]
            # Must have at least one offering in the configured campus/mode.
            matching = [
                o for o in course.offerings
                if o.campus == self.campus and o.mode == self.mode
            ]
            if not matching:
                continue
            # When no_summer=True, SS-only courses can never be scheduled.
            if self.no_summer and all(o.semester == "SS" for o in matching):
                continue
            # Restriction check: permanently blocked if any known restriction done
            if course.restrictions:
                known_restrictions = course.restrictions & self._known_codes
                if known_restrictions & completed:
                    continue
            if prereqs_met(course.prerequisites, completed, self._known_codes):
                return True
        return False
