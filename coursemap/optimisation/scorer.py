"""
Degree plan scoring.

Lower score = better plan. The scoring function is used to select the best
plan when PlanSearch evaluates multiple major matches.

Score components (all additive, lower is better):
  - Semester count * 100:  fewest semesters is the primary objective.
  - Load imbalance * 5:    prefer evenly loaded semesters over spiky distributions.
  - Final semester load * 2: prefer finishing with a lighter final semester.
"""

from __future__ import annotations
from coursemap.domain.plan import DegreePlan


class PlanScorer:
    """
    Scores a DegreePlan. Lower is better.

    Used by PlanSearch to select the best plan when multiple majors match a
    partial name query. The score is not exposed to the user.
    """

    def score(self, plan: DegreePlan) -> float:
        if not plan.semesters:
            return float("inf")

        loads = [s.total_credits() for s in plan.semesters]

        semester_penalty = len(loads) * 100
        spread_penalty   = (max(loads) - min(loads)) * 5
        final_penalty    = loads[-1] * 2

        # Penalise imbalance between S1 and S2 semesters separately.
        # A plan that front-loads S1 and leaves light S2 semesters is worse
        # than one with balanced S1/S2 loads.
        s1_loads = [s.total_credits() for s in plan.semesters if s.semester == "S1"]
        s2_loads = [s.total_credits() for s in plan.semesters if s.semester == "S2"]
        if s1_loads and s2_loads:
            s1_avg = sum(s1_loads) / len(s1_loads)
            s2_avg = sum(s2_loads) / len(s2_loads)
            balance_penalty = abs(s1_avg - s2_avg)
        else:
            balance_penalty = 0.0

        return semester_penalty + spread_penalty + final_penalty + balance_penalty
