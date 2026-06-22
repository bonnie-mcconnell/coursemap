"""
coursemap -- command-line degree planner for Massey University.

Subcommands:
    majors    List available majors (with optional name search).
    courses   Browse the course catalogue.
    plan      Generate a semester-by-semester degree plan.
    validate  Check dataset integrity.

Examples:
    coursemap majors --search "computer science"
    coursemap plan --major "Mental Health and Addiction" --start-year 2025
    coursemap plan --major "Computer Science" --completed 159101,160101
    coursemap plan --major "English" --max-credits 30
    coursemap plan --major "Ecology" --campus M --mode INT
    coursemap validate
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

from coursemap.domain.course import Course
from coursemap.domain.plan import DegreePlan
from coursemap.validation.dataset_validator import validate_dataset
from coursemap.ingestion.dataset_loader import load_courses, load_majors
from coursemap.services.planner_service import PlannerService
from coursemap.domain.prerequisite import (
    AndExpression,
    CoursePrerequisite,
    OrExpression,
    PrerequisiteExpression,
)
from coursemap.domain.prerequisite_utils import prereqs_met


# ---------------------------------------------------------------------------
# coursemap majors
# ---------------------------------------------------------------------------

def _cmd_majors(args: argparse.Namespace) -> None:
    majors = load_majors()

    if args.search:
        results = _search_majors(majors, args.search)
        if not results:
            print(f"No majors matching '{args.search}'.", file=sys.stderr)
            sys.exit(1)
    else:
        results = majors

    results = sorted(results, key=lambda m: m["name"])
    print(f"{len(results)} major(s):\n")
    for m in results:
        print(f"  {m['name']}")
    print()


def _search_majors(majors: list[dict], query: str) -> list[dict]:
    """
    Return majors whose name matches query using word-overlap scoring.

    Each word in the query is checked against the major name. A major matches
    when ALL query words appear as substrings of some word in the name, so
    'comp sci' matches 'Computer Science' and 'maths' matches 'Mathematics'.
    Falls back to whole-string substring if word-overlap finds nothing.
    """
    words = query.strip().lower().split()

    def word_match(name: str) -> bool:
        tokens = name.lower().replace("–", " ").split()
        return all(any(qw in tok for tok in tokens) for qw in words)

    results = [m for m in majors if word_match(m["name"])]
    if results:
        return results
    q = query.strip().lower()
    return [m for m in majors if q in m["name"].lower()]


# ---------------------------------------------------------------------------
# coursemap courses
# ---------------------------------------------------------------------------

def _cmd_courses(args: argparse.Namespace) -> None:
    courses = load_courses()
    results = list(courses.values())

    if args.level is not None:
        results = [c for c in results if c.level == args.level]

    if args.search:
        words = args.search.strip().lower().split()
        results = [c for c in results if all(w in c.title.lower() for w in words)]

    campus = getattr(args, "campus", None)
    mode   = getattr(args, "mode", None)
    if campus:
        results = [c for c in results if any(o.campus == campus for o in c.offerings)]
    if mode:
        results = [c for c in results if any(o.mode == mode for o in c.offerings)]

    semester_filter = getattr(args, "semester", None)
    if semester_filter:
        results = [c for c in results if any(
            o.semester == semester_filter.upper() for o in c.offerings
        )]

    show_inactive = getattr(args, "show_inactive", False)
    if not show_inactive:
        results = [c for c in results if c.offerings]
        # Also exclude zero-credit practicum/placement courses from the default view
        results = [c for c in results if c.credits > 0]

    if not results:
        hint = ""
        if not show_inactive and not (campus or mode or args.level or args.search):
            hint = " (use --show-inactive to include retired courses)"
        print(f"No courses match the given filters.{hint}", file=sys.stderr)
        sys.exit(1)

    any_filter = args.level or args.search or campus or mode
    if not any_filter and not show_inactive:
        print(
            f"Showing {len(results)} active courses. "
            "Use --level, --search, --campus, or --mode to filter.\n"
        )
    else:
        print(f"{len(results)} course(s):\n")

    results.sort(key=lambda c: (c.level, c.code))

    def offering_label(c: Course) -> str:
        if not c.offerings:
            return "[no offerings]"
        if campus and mode:
            sems = sorted({o.semester for o in c.offerings
                           if o.campus == campus and o.mode == mode})
            return f"[{', '.join(sems)}]" if sems else "[not available]"
        dis  = sorted({o.semester for o in c.offerings if o.mode == "DIS"})
        int_ = sorted({o.semester for o in c.offerings if o.mode == "INT"})
        parts = []
        if dis:
            parts.append("DIS:" + "/".join(dis))
        if int_:
            parts.append("INT:" + "/".join(int_))
        other = sorted({o.semester for o in c.offerings
                        if o.mode not in ("DIS", "INT")})
        if other:
            parts.append("/".join(other))
        return f"[{', '.join(parts)}]"

    for c in results:
        label = offering_label(c)
        title = c.title if len(c.title) <= 44 else c.title[:41] + "..."
        print(f"  {c.code}  L{c.level}  {c.credits:>3}cr  {label:<22}  {title}")
    print()


# ---------------------------------------------------------------------------
# coursemap plan  (helpers)
# ---------------------------------------------------------------------------

def _parse_code_list(raw: str, catalogue: dict) -> tuple[frozenset, list]:
    # Normalise: strip whitespace and uppercase (matches server.py sanitisation)
    codes   = [c.strip().upper() for c in raw.split(",") if c.strip()]
    valid   = frozenset(c for c in codes if c in catalogue)
    invalid = [c for c in codes if c not in catalogue]
    return valid, invalid


def _collect_missing(
    prereq_expr: PrerequisiteExpression | None,
    completed: set[str],
    known: frozenset[str],
    planned: set[str],
    out: list[str],
) -> None:
    """
    Recursively collect prerequisite codes that are unmet and not already planned.

    For OR expressions: if any branch is fully satisfied (no missing codes), the
    OR is satisfied and nothing is added. If no branch is fully satisfied, report
    the missing codes from the branch with the fewest missing codes (the "best"
    branch to satisfy).
    """
    if prereq_expr is None:
        return
    if isinstance(prereq_expr, CoursePrerequisite):
        code = prereq_expr.code
        if code in known and code not in completed and code not in planned:
            out.append(code)
        return
    if isinstance(prereq_expr, AndExpression):
        for child in prereq_expr.children:
            _collect_missing(child, completed, known, planned, out)
        return
    if isinstance(prereq_expr, OrExpression):
        # Collect missing from each branch
        branch_missing: list[list[str]] = []
        for child in prereq_expr.children:
            branch: list[str] = []
            _collect_missing(child, completed, known, planned, branch)
            if not branch:
                return  # this branch is fully satisfied - OR is satisfied
            branch_missing.append(branch)
        # No branch fully satisfied: report missing from the branch with fewest gaps
        if branch_missing:
            best = min(branch_missing, key=len)
            out.extend(best)


def _elective_suggestions(
    courses: dict,
    plan: DegreePlan,
    gap: int,
    campus: str,
    mode: str,
) -> list[tuple[int, str, str]]:
    """
    Return (level, code, title) tuples for suggested free-elective courses.
    Thin wrapper around the shared select_free_electives utility.
    """
    from coursemap.domain.requirement_utils import select_free_electives
    planned_codes = {c.code for s in plan.semesters for c in s.courses}
    prior_codes   = {c.code for c in plan.prior_completed}
    return select_free_electives(
        courses=courses,
        planned_codes=planned_codes,
        excluded_codes=prior_codes,
        gap=gap,
        campus=campus,
        mode=mode,
    )


def _print_elective_suggestions(
    courses: dict,
    plan: DegreePlan,
    gap: int,
    campus: str,
    mode: str,
) -> None:
    suggestions = _elective_suggestions(courses, plan, gap, campus, mode)
    if suggestions:
        print("  Suggested electives from your subject area:")
        for level, code, title in suggestions:
            course = courses[code]
            title_short = title if len(title) <= 48 else title[:45] + "..."
            sems = sorted({o.semester for o in course.offerings
                           if o.campus == campus and o.mode == mode})
            sem_str = "/".join(sems) if sems else "?"
            print(f"    {code}  L{level}  {course.credits:>3}cr  [{sem_str}]  {title_short}")
        remaining = gap - sum(courses[code].credits for _, code, _ in suggestions)
        if remaining > 0:
            print(
                f"  + {remaining}cr from any approved Massey courses. "
                "Run 'coursemap courses --search <topic>' to browse."
            )
    else:
        print(
            "  Choose any approved Massey courses. "
            "Run 'coursemap courses --search <topic>' to browse options."
        )


def _print_explain(
    courses: dict,
    plan: "DegreePlan",
    campus: str,
    mode: str,
    prior_completed: frozenset,
) -> None:
    """
    Print a summary of courses that were excluded from the plan and why.
    Groups exclusions by reason: no offering, campus mismatch, or prerequisite issues.
    """
    planned_codes = {c.code for s in plan.semesters for c in s.courses}
    all_scheduled = planned_codes | set(prior_completed)

    no_offering:    list[tuple[str, str]] = []
    wrong_delivery: list[tuple[str, str]] = []

    for code, course in courses.items():
        if code in all_scheduled:
            continue
        if not course.offerings:
            no_offering.append((code, course.title))
        elif not any(o.campus == campus and o.mode == mode for o in course.offerings):
            wrong_delivery.append((code, course.title))

    print("\n--- Explain: excluded courses (sample) ---")
    if no_offering:
        print(f"\nNo offerings at all ({len(no_offering)} courses, sample):")
        for code, title in no_offering[:5]:
            t = title if len(title) <= 50 else title[:47] + "..."
            print(f"  {code}  {t}")

    if wrong_delivery:
        print(f"\nNo {mode} offering at campus {campus!r} ({len(wrong_delivery)} courses, sample):")
        for code, title in wrong_delivery[:5]:
            t = title if len(title) <= 50 else title[:47] + "..."
            avail = sorted({f"{o.campus}/{o.mode}" for o in courses[code].offerings})
            print(f"  {code}  {t}  [available: {', '.join(avail[:3])}]")
    print()


def _export_plan_html(
    plan: "DegreePlan",
    major_label: str,
    gap: int,
    elective_suggestions: list,
    campus: str,
    mode: str,
    start_year: int,
    courses: dict,
    output_arg: str | None,
) -> Path:
    """Export the degree plan as a self-contained HTML file."""
    semesters_html = ""
    for i, semester in enumerate(plan.semesters):
        rows = ""
        for course in semester.courses:
            title = course.title
            fy_badge = ' <span style="font-size:10px;background:#fef3c7;color:#92400e;padding:1px 5px;border-radius:3px;margin-left:4px">FY</span>' if any(getattr(o, "full_year", False) for o in course.offerings) else ""
            prereq_dot = '<span style="color:#e74c3c;font-weight:700;margin-right:4px" title="No prerequisite data - verify before enrolling">•</span>' if course.prerequisites is None and course.level >= 200 else ""
            rows += f"""
            <tr>
              <td class="code">{course.code}</td>
              <td class="title">{prereq_dot}{title}{fy_badge}</td>
              <td class="credits">{course.credits}cr</td>
            </tr>"""
        sem_label = f"{semester.year} {semester.semester}"
        sem_cr = semester.total_credits()
        color_idx = i % 6
        semesters_html += f"""
        <div class="semester sem-color-{color_idx}">
          <div class="sem-header">
            <span class="sem-label">{sem_label}</span>
            <span class="sem-credits">{sem_cr} credits</span>
          </div>
          <table><tbody>{rows}</tbody></table>
        </div>"""

    electives_html = ""
    if gap > 0 and not elective_suggestions:
        # Show gap note even when no suggestions found
        electives_html = f"""
        <section class="electives">
          <h2>Free Elective Gap &mdash; {gap}cr</h2>
          <p style="color:#6b7280;font-size:0.88rem;padding:1rem">
            {gap}cr of free electives are required. Choose any approved Massey courses
            to fill this gap. Use <code>coursemap courses --search &lt;topic&gt;</code>
            to browse options.
          </p>
        </section>"""
    if gap > 0 and elective_suggestions:
        rows = ""
        for level, code, title in elective_suggestions:
            cr = courses[code].credits
            sems = sorted({o.semester for o in courses[code].offerings
                           if o.campus == campus and o.mode == mode})
            sem_str = "/".join(sems)
            rows += f"""
            <tr>
              <td class="code">{code}</td>
              <td class="title">{title}</td>
              <td class="credits">{cr}cr</td>
              <td class="sem-tag">[{sem_str}]</td>
            </tr>"""
        electives_html = f"""
        <section class="electives">
          <h2>Suggested Free Electives <span class="gap-badge">{gap}cr needed</span></h2>
          <table><tbody>{rows}</tbody></table>
        </section>"""

    summary_cr = plan.total_credits()
    prior_cr   = plan.prior_credits()
    total_cr   = summary_cr + prior_cr
    sems       = len(plan.semesters)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Degree Plan – {major_label}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;600;700&display=swap');

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:        #f4f2ee;
    --surface:   #ffffff;
    --border:    #e0ddd7;
    --text:      #1a1916;
    --muted:     #6b6860;
    --accent:    #c84b31;
    --mono:      'DM Mono', monospace;
    --sans:      'DM Sans', sans-serif;
    --c0: #e8f4f0; --c1: #f0eaf8; --c2: #fef3e2;
    --c3: #e6f0fa; --c4: #fde8e8; --c5: #e8f0e8;
    --c0b: #2d8a6e; --c1b: #7c5cbf; --c2b: #c47d10;
    --c3b: #2563a8; --c4b: #c03030; --c5b: #2e7d32;
  }}

  body {{
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 2rem;
  }}

  .page-wrap {{ max-width: 860px; margin: 0 auto; }}

  header {{
    border-bottom: 2px solid var(--text);
    padding-bottom: 1.5rem;
    margin-bottom: 2.5rem;
  }}

  .university-tag {{
    font-family: var(--mono);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.15em;
    color: var(--muted);
    margin-bottom: 0.5rem;
  }}

  h1 {{
    font-size: clamp(1.4rem, 3vw, 2rem);
    font-weight: 700;
    line-height: 1.2;
    margin-bottom: 0.75rem;
  }}

  .meta-row {{
    display: flex;
    gap: 1.5rem;
    flex-wrap: wrap;
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--muted);
  }}

  .meta-row span {{ display: flex; align-items: center; gap: 0.4rem; }}

  .summary-bar {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 1px;
    background: var(--border);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 2.5rem;
  }}

  .summary-cell {{
    background: var(--surface);
    padding: 1rem 1.25rem;
  }}

  .summary-cell .val {{
    font-size: 1.6rem;
    font-weight: 700;
    line-height: 1;
    font-family: var(--mono);
  }}

  .summary-cell .lbl {{
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-top: 0.25rem;
  }}

  .semesters {{ display: flex; flex-direction: column; gap: 1rem; margin-bottom: 2.5rem; }}

  .semester {{
    border-radius: 8px;
    overflow: hidden;
    border: 1px solid var(--border);
  }}

  .sem-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.6rem 1rem;
    font-family: var(--mono);
    font-size: 0.8rem;
    font-weight: 500;
  }}

  .sem-color-0 .sem-header {{ background: var(--c0); color: var(--c0b); }}
  .sem-color-1 .sem-header {{ background: var(--c1); color: var(--c1b); }}
  .sem-color-2 .sem-header {{ background: var(--c2); color: var(--c2b); }}
  .sem-color-3 .sem-header {{ background: var(--c3); color: var(--c3b); }}
  .sem-color-4 .sem-header {{ background: var(--c4); color: var(--c4b); }}
  .sem-color-5 .sem-header {{ background: var(--c5); color: var(--c5b); }}

  .sem-label {{ font-weight: 600; font-size: 0.85rem; }}
  .sem-credits {{ opacity: 0.8; }}

  table {{ width: 100%; border-collapse: collapse; background: var(--surface); }}

  tr {{ border-top: 1px solid var(--border); }}
  tr:first-child {{ border-top: none; }}

  td {{ padding: 0.6rem 1rem; vertical-align: middle; }}

  td.code {{
    font-family: var(--mono);
    font-size: 0.78rem;
    color: var(--muted);
    white-space: nowrap;
    width: 80px;
  }}

  td.title {{ font-size: 0.88rem; }}

  td.credits {{
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--muted);
    text-align: right;
    white-space: nowrap;
    width: 50px;
  }}

  td.sem-tag {{
    font-family: var(--mono);
    font-size: 0.72rem;
    color: var(--muted);
    text-align: right;
    white-space: nowrap;
    width: 80px;
  }}

  .electives {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
    margin-bottom: 2.5rem;
  }}

  .electives h2 {{
    font-size: 0.9rem;
    font-weight: 600;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }}

  .gap-badge {{
    font-family: var(--mono);
    font-size: 0.7rem;
    background: var(--c2);
    color: var(--c2b);
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-weight: 500;
  }}

  footer {{
    border-top: 1px solid var(--border);
    padding-top: 1rem;
    font-size: 0.72rem;
    color: var(--muted);
    font-family: var(--mono);
  }}

  @media print {{
    body {{ background: white; padding: 1rem; }}
    .semester {{ break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="page-wrap">
  <header>
    <div class="university-tag">Massey University · coursemap</div>
    <h1>{major_label}</h1>
    <div class="meta-row">
      <span>Starting {start_year}</span>
      <span>Delivery {campus}/{mode}</span>
    </div>
  </header>

  <div class="summary-bar">
    <div class="summary-cell"><div class="val">{sems}</div><div class="lbl">Semesters</div></div>
    <div class="summary-cell"><div class="val">{summary_cr}</div><div class="lbl">Credits Planned</div></div>
    <div class="summary-cell"><div class="val">{total_cr}</div><div class="lbl">Credits Total</div></div>
    {"" if not prior_cr else f'<div class="summary-cell"><div class="val">{prior_cr}</div><div class="lbl">Credits Prior</div></div>'}
  </div>

  <div class="semesters">{semesters_html}</div>

  {electives_html}

  <footer>Generated by coursemap · Verify all requirements with Massey University before enrolling.</footer>
</div>
</body>
</html>"""

    suffix = ".html"
    if output_arg:
        output_path = Path(output_arg).with_suffix(suffix)
    else:
        output_path = Path("plan.html")
    tmp = output_path.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, output_path)
    return output_path


# ---------------------------------------------------------------------------
# coursemap plan
# ---------------------------------------------------------------------------

def _export_plan_json(
    plan,
    output_arg,
    *,
    major_label="",
    campus="D",
    mode="DIS",
    start_year=2026,
    gap=0,
    degree_total=0,
    filler_codes=None,
    double_info=None,
):
    """Serialise plan to JSON with rich metadata. Writes atomically."""
    semesters_data = [
        {
            "year":     s.year,
            "semester": s.semester,
            "credits":  s.total_credits(),
            "courses": [
                {
                    "code": c.code, "title": c.title, "credits": c.credits,
                    "full_year": any(getattr(o, "full_year", False) for o in c.offerings),
                    "prereq_data_available": c.prerequisites is not None,
                }
                for c in s.courses
            ],
        }
        for s in plan.semesters
    ]
    meta = {
        "major":             major_label,
        "campus":            campus,
        "mode":              mode,
        "start_year":        start_year,
        "credits_planned":   plan.total_credits(),
        "credits_prior":     plan.prior_credits(),
        "credits_transfer":  plan.transfer_credits,
        "credits_total":     plan.total_credits() + plan.prior_credits() + plan.transfer_credits,
        "degree_target":     degree_total,
        "free_elective_gap": gap,
    }
    if filler_codes:
        meta["auto_filled_codes"] = filler_codes
    if double_info:
        meta["double_major"] = {
            "first":         double_info["first_label"],
            "second":        double_info["second_label"],
            "shared_codes":  sorted(double_info["shared_codes"]),
            "saved_credits": double_info["saved_credits"],
        }
    if plan.prior_completed:
        meta["prior_completed"] = [
            {"code": c.code, "title": c.title, "credits": c.credits}
            for c in plan.prior_completed
        ]
    output_path = Path(output_arg) if output_arg else Path("plan.json")
    tmp = output_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"meta": meta, "semesters": semesters_data}, indent=2), encoding="utf-8")
    os.replace(tmp, output_path)
    return output_path


def _audit_prereqs(
    plan: DegreePlan,
    prior_completed: frozenset[str],
    courses: dict,
) -> tuple[list[str], list[str]]:
    """
    Walk the plan in semester order and report courses whose in-plan prerequisites
    were not completed in a prior semester.

    Phantom codes are stripped at load time, so any result here reflects a genuine
    ordering concern in the scraped data rather than noise.

    Returns (notes, missing) where notes is the list of affected course codes and
    missing is the deduplicated list of unmet prerequisite codes.
    """
    planned_codes: set[str] = {c.code for sem in plan.semesters for c in sem.courses}
    known_scope = frozenset(planned_codes) | frozenset(prior_completed)
    completed_set: set[str] = set(prior_completed)
    notes: list[str] = []
    missing: list[str] = []

    for sem in plan.semesters:
        for c in sem.courses:
            co = courses.get(c.code)
            if co and co.prerequisites:
                if not prereqs_met(co.prerequisites, completed_set, known_scope):
                    notes.append(c.code)
                    _collect_missing(
                        co.prerequisites, completed_set, known_scope,
                        planned_codes, missing,
                    )
        completed_set.update(c.code for c in sem.courses)

    seen: set[str] = set()
    missing = [c for c in missing if not (c in seen or seen.add(c))]
    return notes, missing


def _gap_desc(gap: int) -> str:
    """Human-readable description of a credit gap (e.g. '2 full semester(s) + 15cr')."""
    full_sems  = gap // 60
    part_creds = gap % 60
    if full_sems and part_creds:
        return f"{full_sems} full semester(s) + {part_creds}cr"
    if full_sems:
        return f"{full_sems} full semester(s)"
    return f"{part_creds} additional credits"



def _print_double_major_info(info: dict) -> None:
    """Print shared-course summary for a double-major plan."""
    shared = info["shared_codes"]
    saved  = info["saved_credits"]
    if shared:
        codes_str = ", ".join(sorted(shared)[:6])
        suffix    = f" and {len(shared)-6} more" if len(shared) > 6 else ""
        print(
            f"\nDouble major: {len(shared)} shared course(s) save {saved}cr of overlap "
            f"({codes_str}{suffix})."
        )
    else:
        print("\nDouble major: no shared courses between the two majors.")


def _print_plan_header(
    major_label: str,
    start_year: int,
    campus: str,
    mode: str,
    prior_completed: frozenset,
    plan: "DegreePlan",
) -> None:
    """Print the plan title block, prior-completed, and transfer-credits notices."""
    print(f"\nDegree plan: {major_label}")
    print(f"Starting:    {start_year}  |  Delivery: {campus}/{mode}")
    if prior_completed:
        codes_str = ", ".join(sorted(prior_completed))
        print(f"Completed:   {codes_str} ({plan.prior_credits()} credits)")
    if plan.transfer_credits > 0:
        print(f"Transfer:    {plan.transfer_credits} credits recognised from prior learning")


def _print_exclusion_warnings(
    excl: list[str],
    mode: str,
    campus: str,
    student_excl_required: list[str] | None = None,
) -> None:
    """Print notice about campus-excluded required courses."""
    if not excl:
        return
    codes_str = ", ".join(excl[:5]) + (f" and {len(excl)-5} more" if len(excl) > 5 else "")
    print(
        f"\nNote: {len(excl)} required course(s) have no {mode} offering and "
        f"are excluded from this plan: {codes_str}."
    )
    if campus == "D":
        print(
            "      Some courses in this major may require on-campus attendance. "
            "Use --campus M --mode INT for a Manawatu-based plan."
        )


def _print_prereq_warnings(
    prereq_notes: list[str],
    prereq_missing: list[str],
) -> None:
    """Print prerequisite ordering warnings when the audit finds gaps."""
    if not prereq_notes:
        return
    planned_str = (
        ", ".join(prereq_notes[:5])
        + (f" and {len(prereq_notes) - 5} more" if len(prereq_notes) > 5 else "")
    )
    if prereq_missing:
        missing_str = (
            ", ".join(prereq_missing[:4])
            + (f" and {len(prereq_missing) - 4} more" if len(prereq_missing) > 4 else "")
        )
        print(
            f"\nNote: {len(prereq_notes)} course(s) in this plan may require prior study:\n"
            f"      {planned_str}\n"
            f"      Possible missing prerequisites: {missing_str}\n"
            "      Confirm enrolment eligibility with Massey before registering."
        )
    else:
        print(
            f"\nNote: {len(prereq_notes)} course(s) reference prerequisites not in "
            f"this plan (treated as satisfied): {planned_str}.\n"
            "      Confirm with Massey Student Services."
        )


def _print_semester_table(plan: "DegreePlan") -> None:
    """Print the semester-by-semester course table."""
    print()
    for semester in plan.semesters:
        print(f"{semester.year} {semester.semester}  ({semester.total_credits()} credits)")
        for course in semester.courses:
            title = course.title if len(course.title) <= 48 else course.title[:45] + "..."
            print(f"   {course.code}  {course.credits:2}cr  {title}")
        print()


def _print_summary(
    plan: "DegreePlan",
    gap: int,
    degree_total: int,
    auto_fill: bool,
    filler_codes: list[str],
    courses: dict,
    has_major: bool,
) -> None:
    """Print the Summary block and major requirements line."""
    planned_cr   = plan.total_credits()
    prior_cr     = plan.prior_credits()
    transfer_cr  = plan.transfer_credits
    total_cr     = planned_cr + prior_cr + transfer_cr
    sems         = len(plan.semesters)
    filled_cr    = sum(courses[c].credits for c in filler_codes if c in courses)
    remaining_gap = max(0, gap - filled_cr)

    # effective_gap: credits still needed beyond what's scheduled + prior + transfer
    effective_gap = max(0, gap - transfer_cr)

    print("Summary")
    print(f"  Semesters planned : {sems}")
    print(f"  Credits planned   : {planned_cr}")
    if prior_cr:
        print(f"  Credits prior     : {prior_cr}")
    if transfer_cr:
        print(f"  Credits transfer  : {transfer_cr}")
    print(f"  Credits total     : {total_cr}")

    if effective_gap > 0:
        if auto_fill and filler_codes:
            remaining = max(0, remaining_gap - transfer_cr)
            label = f"({remaining}cr still to self-select)" if remaining > 0 else "(auto-filled)"
        else:
            label = f"({effective_gap}cr free electives to self-select)"
        print(f"  Degree target     : {degree_total}cr  {label}")
    elif gap > 0 and transfer_cr >= gap:
        print(f"  Degree target     : {degree_total}cr  (transfer credits cover remaining gap)")

    if has_major:
        print("\nMajor requirements: satisfied")


def _print_elective_section(
    gap: int,
    auto_fill: bool,
    filler_codes: list[str],
    courses: dict,
    plan: "DegreePlan",
    campus: str,
    mode: str,
    degree_total: int,
) -> None:
    """Print the free-elective section: either auto-fill summary or suggestions."""
    if gap <= 0:
        return
    filled_cr     = sum(courses[c].credits for c in filler_codes if c in courses)
    remaining_gap = max(0, gap - filled_cr)

    if auto_fill and filler_codes:
        print(
            f"\nFree electives: {gap}cr gap auto-filled with {len(filler_codes)} "
            f"subject-area course(s) ({filled_cr}cr):"
        )
        for code in filler_codes:
            if code not in courses:
                continue
            course = courses[code]
            sems_avail = sorted({o.semester for o in course.offerings
                                 if o.campus == campus and o.mode == mode})
            sem_str = "/".join(sems_avail) if sems_avail else "?"
            title   = course.title if len(course.title) <= 48 else course.title[:45] + "..."
            print(f"    {code}  L{course.level}  {course.credits:>3}cr  [{sem_str}]  {title}")
        if remaining_gap > 0:
            print(
                f"  + {remaining_gap}cr still to self-select. "
                "Run 'coursemap courses --search <topic>' to browse."
            )
    else:
        print(
            f"\nFree electives needed: {gap}cr ({_gap_desc(gap)}) "
            f"to reach {degree_total}cr total."
        )
        _print_elective_suggestions(courses, plan, gap, campus, mode)


# ---------------------------------------------------------------------------
# coursemap plan  (main handler)
# ---------------------------------------------------------------------------

def _cmd_minors(args: argparse.Namespace) -> None:
    """List available minors, optionally filtered by name."""
    from coursemap.ingestion.minor_loader import load_minors, search_minors
    minors = load_minors()
    if args.search:
        minors = search_minors(args.search, minors)
    if not minors:
        print(f"No minors matching '{args.search}'." if args.search else "No minors found.",
              file=sys.stderr)
        sys.exit(1)
    quality_filter = getattr(args, "quality", None)
    if quality_filter:
        minors = [m for m in minors if m.get("data_quality") == quality_filter]
    print(f"{len(minors)} minor(s):\n")
    for m in sorted(minors, key=lambda x: x["name"]):
        quality = m.get("data_quality", "?")
        credits = m.get("total_credits", "?")
        flag = "  ⚠ inferred" if quality == "inferred" else ""
        print(f"  {m['name']:<45} {credits}cr{flag}")
    print()
    if any(m.get("data_quality") == "inferred" for m in minors):
        print("⚠ Inferred minors are approximated from course patterns. "
              "Verify at massey.ac.nz/study/minors/")


def _cmd_plan(args: argparse.Namespace) -> None:
    courses = load_courses()
    majors  = load_majors()
    svc     = PlannerService(courses, majors)

    # ---- Resolve major label -----------------------------------------------
    if args.major:
        try:
            resolved    = svc.resolve_major(args.major)
            major_label = resolved[0]["name"] if len(resolved) == 1 else f"{len(resolved)} majors"
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        major_label = "all majors"

    # ---- Parse --completed / --prefer code lists ---------------------------
    prior_completed = frozenset()
    if args.completed:
        prior_completed, invalid = _parse_code_list(args.completed, courses)
        if invalid:
            print(
                f"Warning: {len(invalid)} unrecognised code(s) ignored: "
                + ", ".join(sorted(invalid)),
                file=sys.stderr,
            )

    preferred = frozenset()
    if args.prefer:
        preferred, invalid = _parse_code_list(args.prefer, courses)
        if invalid:
            print(
                f"Warning: {len(invalid)} unrecognised preferred code(s) ignored: "
                + ", ".join(sorted(invalid)),
                file=sys.stderr,
            )

    excluded = frozenset()
    if getattr(args, "exclude", None):
        excluded, invalid = _parse_code_list(args.exclude, courses)
        if invalid:
            print(
                f"Warning: {len(invalid)} unrecognised excluded code(s) ignored: "
                + ", ".join(sorted(invalid)),
                file=sys.stderr,
            )

    # ---- Resolve --minor flag (add minor courses to preferred electives) ----
    minor_name = getattr(args, "minor", None)
    if minor_name:
        from coursemap.ingestion.minor_loader import load_minors as _lm, search_minors as _sm
        _minors = _lm()
        _matches = _sm(minor_name, _minors)
        if not _matches:
            print(
                f"Warning: Minor '{minor_name}' not found. "
                "Run 'coursemap minors' to see available minors.",
                file=sys.stderr,
            )
        else:
            _minor = _matches[0]
            if len(_matches) > 1:
                print(f"Minor: matched '{_minor['name']}' (first of {len(_matches)} matches).", file=sys.stderr)
            else:
                print(f"Minor: adding '{_minor['name']}' courses to preferred electives.", file=sys.stderr)
            # Collect course codes from the minor requirement dict
            def _minor_codes(req_dict: dict) -> set:
                t = req_dict.get("type", "")
                if t == "COURSE":
                    return {req_dict["course_code"]}
                if t == "CHOOSE_CREDITS":
                    return set(req_dict.get("course_codes", []))
                if t in ("ALL_OF", "ANY_OF"):
                    result = set()
                    for child in req_dict.get("children", []):
                        result |= _minor_codes(child)
                    return result
                return set()
            _minor_prefer = frozenset(
                c for c in _minor_codes(_minor.get("requirement", {})) if c in courses
            )
            preferred = preferred | _minor_prefer
            print(f"         Added {len(_minor_prefer)} course(s) to preferred electives.", file=sys.stderr)

    # ---- Resolve start year / semester defaults ----------------------------
    import datetime as _dt
    _now = _dt.date.today()
    if args.start_year is None:
        args.start_year = _now.year
    if getattr(args, "start_semester", None) is None:
        # Auto-detect current semester from month: Feb-Jun → S1, Jul-Oct → S2, Nov-Jan → SS
        _m = _now.month
        args.start_semester = "S1" if _m <= 6 else ("S2" if _m <= 10 else "SS")

    # ---- Generate plan -----------------------------------------------------
    double_major = getattr(args, "double_major", None)
    auto_fill    = getattr(args, "auto_fill", False)
    double_info: dict | None = None

    # Warn early if the student has excluded required courses. The plan will
    # still be generated (the planner skips excluded courses) but validation
    # will report unsatisfied requirements. The warning here is printed before
    # generation so it always appears even when validation later fails.
    if excluded and args.major and not double_major:
        _prereq_excl = svc.student_excluded_required_courses(
            args.major, excluded, campus=args.campus, mode=args.mode
        )
        if _prereq_excl:
            codes_str = ", ".join(_prereq_excl[:5]) + (
                f" and {len(_prereq_excl)-5} more" if len(_prereq_excl) > 5 else ""
            )
            print(
                f"Warning: {len(_prereq_excl)} excluded course(s) are required by this "
                f"degree and cannot be omitted: {codes_str}.\n"
                "         The plan will be generated without them but major requirements "
                "will NOT be satisfied.\n"
                "         Consider using --prefer to swap electives instead.",
                file=sys.stderr,
            )

    try:
        if double_major and auto_fill and args.major:
            plan, double_info, _filler = svc.generate_filled_double_major_plan(
                major_name=args.major,
                second_major_name=double_major,
                max_credits_per_semester=args.max_credits,
                max_courses_per_semester=args.max_per_semester,
                campus=args.campus,
                mode=args.mode,
                start_year=args.start_year,
                start_semester=getattr(args, "start_semester", None),
                prior_completed=prior_completed,
                preferred_electives=preferred,
                excluded_courses=excluded,
                no_summer=args.no_summer,
                transfer_credits=getattr(args, "transfer_credits", 0),
            )
            filler_codes = list(_filler)
            major_label = f"{double_info['first_label']}  +  {double_info['second_label']}"
        elif double_major and args.major:
            plan, double_info = svc.generate_double_major_plan(
                major_name=args.major,
                second_major_name=double_major,
                max_credits_per_semester=args.max_credits,
                max_courses_per_semester=args.max_per_semester,
                campus=args.campus,
                mode=args.mode,
                start_year=args.start_year,
                start_semester=getattr(args, "start_semester", None),
                prior_completed=prior_completed,
                preferred_electives=preferred,
                excluded_courses=excluded,
                no_summer=args.no_summer,
                transfer_credits=getattr(args, "transfer_credits", 0),
            )
            filler_codes = []
            # Override major_label for double-major display
            major_label = f"{double_info['first_label']}  +  {double_info['second_label']}"
        elif auto_fill and args.major:
            plan, filler_codes = svc.generate_filled_plan(
                major_name=args.major,
                max_credits_per_semester=args.max_credits,
                max_courses_per_semester=args.max_per_semester,
                campus=args.campus,
                mode=args.mode,
                start_year=args.start_year,
                start_semester=getattr(args, "start_semester", None),
                prior_completed=prior_completed,
                preferred_electives=preferred,
                excluded_courses=excluded,
                no_summer=args.no_summer,
                transfer_credits=getattr(args, "transfer_credits", 0),
            )
        else:
            filler_codes = []
            plan = svc.generate_best_plan(
                major_name=args.major,
                max_credits_per_semester=args.max_credits,
                max_courses_per_semester=args.max_per_semester,
                campus=args.campus,
                mode=args.mode,
                start_year=args.start_year,
                start_semester=getattr(args, "start_semester", None),
                prior_completed=prior_completed,
                preferred_electives=preferred,
                excluded_courses=excluded,
                no_summer=args.no_summer,
                transfer_credits=getattr(args, "transfer_credits", 0),
            )
    except ValueError as exc:
        msg   = str(exc)
        match = re.search(r"No valid plan found\.\s*'[^']*':\s*(.*)", msg, re.DOTALL)
        if match:
            msg = match.group(1).strip()
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    # ---- Compute gap / exclusions ------------------------------------------
    if double_info:
        # For a double major the degree target is the higher of the two qualification
        # totals (since both are completed within one enrolment). The gap is the
        # remaining credits needed beyond what's already scheduled.
        first_total  = svc.degree_total_credits(args.major)
        second_total = svc.degree_total_credits(double_major)
        degree_total = max(first_total, second_total, plan.total_credits() + plan.prior_credits())
        gap          = max(0, degree_total - plan.total_credits() - plan.prior_credits())
        excl         = []
        student_excl_required: list[str] = []
    elif args.major:
        _raw_gap     = svc.free_elective_gap(args.major, campus=args.campus, mode=args.mode)
        excl         = svc.campus_excluded_courses(args.major, campus=args.campus, mode=args.mode)
        degree_total = svc.degree_total_credits(args.major)
        # Residual gap: if the plan has reached the degree total (the generator
        # may have filled with subject-area electives), show 0. Otherwise show
        # the structural gap so the student knows they must self-select courses.
        _plan_total  = plan.total_credits() + plan.prior_credits() + plan.transfer_credits
        gap          = max(0, degree_total - _plan_total)
        student_excl_required = (
            svc.student_excluded_required_courses(
                args.major, excluded, campus=args.campus, mode=args.mode
            )
            if excluded else []
        )
    else:
        gap, excl    = 0, []
        degree_total = plan.total_credits() + plan.prior_credits()
        student_excl_required = []

    # ---- HTML export (early return) ----------------------------------------
    output_format = getattr(args, "output_format", "text")

    if output_format == "html":
        # For auto-fill, show filler as the elective section; otherwise show suggestions.
        if auto_fill and filler_codes:
            html_electives = [
                (courses[c].level, c, courses[c].title)
                for c in filler_codes if c in courses
            ]
        else:
            html_electives = _elective_suggestions(courses, plan, gap, args.campus, args.mode)
        html_path = _export_plan_html(
            plan=plan, major_label=major_label,
            gap=gap if not (auto_fill and filler_codes) else 0,
            elective_suggestions=html_electives,
            campus=args.campus, mode=args.mode,
            start_year=args.start_year, courses=courses, output_arg=args.output,
        )
        _print_plan_header(major_label, args.start_year, args.campus, args.mode,
                           prior_completed, plan)
        if double_info:
            print("\nBoth major requirements: satisfied")
        elif args.major:
            print("\nMajor requirements: satisfied")
        if gap > 0 and not (auto_fill and filler_codes):
            print(f"Free electives needed: {gap}cr ({_gap_desc(gap)}) "
                  f"to reach {degree_total}cr total.")
        print(f"\nHTML plan exported to {html_path}\n")
        return

    # ---- JSON export -------------------------------------------------------
    output_path = _export_plan_json(
        plan, args.output,
        major_label=major_label,
        campus=args.campus,
        mode=args.mode,
        start_year=args.start_year,
        gap=gap,
        degree_total=degree_total,
        filler_codes=filler_codes or None,
        double_info=double_info,
    )

    # --format json: machine-readable output only - suppress terminal display.
    if output_format == "json":
        print(f"Plan written to {output_path}", file=sys.stderr)
        return

    # --format ical: export iCalendar file.
    if output_format == "ical":
        from coursemap.export.ical import plan_to_ical
        ical_path = Path(args.output) if args.output else Path("plan.ics")
        plan_to_ical(plan, major_label, campus=args.campus, mode=args.mode,
                     output_path=ical_path)
        print(f"Calendar exported to {ical_path}", file=sys.stderr)
        return

    # ---- Prerequisite audit ------------------------------------------------
    prereq_notes, prereq_missing = (
        _audit_prereqs(plan, prior_completed, courses) if args.major else ([], [])
    )

    # ---- Terminal output ---------------------------------------------------
    _print_plan_header(major_label, args.start_year, args.campus, args.mode,
                       prior_completed, plan)
    _print_exclusion_warnings(excl, args.mode, args.campus, student_excl_required)

    if double_info:
        _print_double_major_info(double_info)

    _print_prereq_warnings(prereq_notes, prereq_missing)
    _print_semester_table(plan)
    _print_summary(plan, gap, degree_total, auto_fill, filler_codes, courses,
                   bool(args.major) and not double_info)

    if double_info:
        print("\nBoth major requirements: satisfied")
    elif args.major:
        pass  # already printed inside _print_summary
    _print_elective_section(gap, auto_fill, filler_codes, courses, plan,
                            args.campus, args.mode, degree_total)

    print(f"\nFull plan exported to {output_path}\n")

    if getattr(args, "explain", False):
        _print_explain(courses, plan, args.campus, args.mode, prior_completed)

    if args.debug:
        stats = svc.last_plan_stats
        total_scheduled = sum(len(s.courses) for s in plan.semesters)
        print("--- diagnostics ---")
        print(f"courses scheduled      : {total_scheduled}")
        print(f"semesters              : {len(plan.semesters)}")
        if stats:
            print(f"prereq rejections      : {stats.prerequisite_rejections}")
            print(f"offering rejections    : {stats.offering_rejections}")
            print(f"empty semesters        : {stats.empty_semesters_skipped}")
            print(f"rebalance moves        : {stats.rebalance_moves}")
        print()


# ---------------------------------------------------------------------------
# coursemap validate
# ---------------------------------------------------------------------------

def _cmd_validate(args: argparse.Namespace) -> None:
    """Run dataset integrity checks and report results."""
    try:
        courses = load_courses()
        majors  = load_majors()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    result = validate_dataset(courses, majors, raise_on_error=False)

    active     = sum(1 for c in courses.values() if c.offerings)
    with_prereqs = sum(1 for c in courses.values() if c.prerequisites)

    print("Dataset summary")
    print(f"  Courses            : {len(courses)}")
    print(f"  Active             : {active}  (have at least one offering)")
    print(f"  With prerequisites : {with_prereqs}")
    print(f"  Majors             : {len(majors)}")
    print()

    if result.errors:
        print(f"{len(result.errors)} error(s):")
        for e in result.errors[:20]:
            print(f"  ERROR  {e}")
        if len(result.errors) > 20:
            print(f"  ... and {len(result.errors) - 20} more")
    else:
        print("No errors.")

    if result.warnings:
        show_all = getattr(args, "all_warnings", False)
        quiet    = getattr(args, "quiet", False)
        if quiet:
            print(f"\n{len(result.warnings)} warning(s) suppressed (omit --quiet to review).")
        else:
            limit = len(result.warnings) if show_all else 20
            shown = result.warnings[:limit]
            print(f"\n{len(result.warnings)} warning(s) (showing {len(shown)}):")
            for w in shown:
                print(f"  WARN   {w}")
            if not show_all and len(result.warnings) > 20:
                print(f"  ... and {len(result.warnings) - 20} more. Use --all to show all.")

    if result.errors:
        sys.exit(1)



def _cmd_data_quality(args: argparse.Namespace) -> None:
    """Print a structured data quality report for the bundled datasets."""
    import json
    from collections import Counter
    from pathlib import Path
    from coursemap.ingestion.dataset_loader import load_courses, load_majors, DATASET_PATH
    from coursemap.ingestion.freshness import freshness_report as dataset_freshness

    try:
        courses = load_courses()
        majors  = load_majors()
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Freshness ──────────────────────────────────────────────────────────
    fresh = dataset_freshness()
    scrape_date = fresh.get("scrape_date", "unknown")
    is_stale    = fresh.get("is_stale", False)
    stale_label = "  ⚠  STALE - run `coursemap refresh-prerequisites`" if is_stale else "  ✓"

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║              coursemap · Data Quality Report                ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()
    print(f"  Dataset date   : {scrape_date}{stale_label}")
    print()

    # ── Courses ────────────────────────────────────────────────────────────
    total_courses = len(courses)
    active        = sum(1 for c in courses.values() if c.offerings)
    no_offerings  = total_courses - active

    # Prerequisite format distribution (from raw JSON, not loaded domain)
    with open(DATASET_PATH, encoding="utf-8") as f:
        raw_courses = json.load(f)

    fmt_counts: dict[str, int] = Counter()
    for c in raw_courses:
        pval = c.get("prerequisites")
        if pval is None:
            fmt_counts["null (never scraped)"] += 1
        elif isinstance(pval, list):
            fmt_counts["flat list (old scraper)"] += 1
        elif isinstance(pval, dict):
            fmt_counts["AND/OR tree (new scraper)"] += 1
        elif isinstance(pval, str):
            fmt_counts["single code (new scraper)"] += 1
        else:
            fmt_counts[f"unknown ({type(pval).__name__})"] += 1

    new_format = fmt_counts.get("AND/OR tree (new scraper)", 0) + fmt_counts.get("single code (new scraper)", 0)
    old_format = fmt_counts.get("flat list (old scraper)", 0)
    null_format = fmt_counts.get("null (never scraped)", 0)
    coverage_pct = round(100 * new_format / total_courses) if total_courses else 0

    # After loading: how many resolve to real prerequisites?
    with_real_prereqs = sum(1 for c in courses.values() if c.prerequisites is not None)
    null_after_load   = total_courses - with_real_prereqs

    print(f"  Courses (total)     : {total_courses}")
    print(f"  With offerings      : {active}  ({round(100*active/total_courses)}%)")
    print(f"  No offering data    : {no_offerings}  (postgrad / inactive)")
    print()
    print("  Prerequisite data format")
    print(f"    AND/OR structured : {new_format:5d}  ({coverage_pct}%)   ← correct, HTML-aware")
    print(f"    Flat list (old)   : {old_format:5d}  ({round(100*old_format/total_courses)}%)   ← noisy, cross-subject prereqs dropped")
    print(f"    Null              : {null_format:5d}  ({round(100*null_format/total_courses)}%)   ← never scraped")
    print()
    print(f"  After plausibility filter")
    print(f"    Courses with prereqs : {with_real_prereqs}  ({round(100*with_real_prereqs/total_courses)}%)")
    print(f"    Prereqs = None       : {null_after_load}  ({round(100*null_after_load/total_courses)}%)")
    print()

    if old_format > 0 or null_format > 0:
        to_rescrape = old_format + null_format
        est_min = round(to_rescrape / 20 * 0.4 / 60, 1)
        print(f"  ⚠  {to_rescrape} courses need re-scraping to get structured prerequisites.")
        print(f"     Run:  python -m coursemap.ingestion.refresh_prerequisites")
        print(f"     Estimated time: ~{est_min} min at 20 workers")
        print()

    # ── Level distribution ──────────────────────────────────────────────────
    level_dist = Counter(c.level for c in courses.values())
    print("  Level distribution")
    for level in sorted(level_dist):
        bar_width = round(level_dist[level] / total_courses * 30)
        bar = "█" * bar_width
        print(f"    L{level:<4d} : {level_dist[level]:4d}  {bar}")
    print()

    # ── Majors ─────────────────────────────────────────────────────────────
    import re as _re
    def _parse_qual(name: str) -> str:
        parts = _re.split(r"\s+[–--]\s+", name, maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else name

    total_majors = len(majors)
    qual_dist    = Counter(_parse_qual(m["name"]) for m in majors)
    ug_keys      = [k for k in qual_dist if "Bachelor" in k or "Graduate Certificate" in k or "Graduate Diploma" in k]
    pg_keys      = [k for k in qual_dist if k not in ug_keys]
    ug_total     = sum(qual_dist[k] for k in ug_keys)
    pg_total     = sum(qual_dist[k] for k in pg_keys)

    print(f"  Majors (total)      : {total_majors}")
    print(f"    Undergraduate     : {ug_total}")
    print(f"    Postgraduate      : {pg_total}")
    print()
    print("  Top qualification types")
    for qual, count in qual_dist.most_common(8):
        print(f"    {count:3d}  {qual}")
    print()

    # ── Cross-subject prereqs lost to plausibility filter ─────────────────
    cross_subject_lost = 0
    for c in raw_courses:
        code = c.get("course_code", "")
        pval = c.get("prerequisites")
        if isinstance(pval, list):
            for p in pval:
                p_clean = p.replace("Course code:", "").strip()
                if p_clean and p_clean != code and p_clean[:3] != code[:3]:
                    cross_subject_lost += 1
                    break

    if cross_subject_lost > 0:
        print(f"  Cross-subject prereqs lost to plausibility filter: ~{cross_subject_lost} courses")
        print(f"  (These will be recovered when re-scraping with the HTML-aware scraper)")
        print()

    # ── Recommendation summary ─────────────────────────────────────────────
    to_rescrape = old_format + null_format
    print("  Recommendations")
    if new_format == total_courses:
        print("    ✓ All courses use structured prerequisite format.")
    else:
        print(f"    ① Run refresh_prerequisites to upgrade {to_rescrape} courses  [HIGH IMPACT]")
    if is_stale:
        print("    ② Dataset is stale - re-run ingestion to get current offerings")
    else:
        print("    ✓ Dataset freshness OK")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _cmd_serve(args: argparse.Namespace) -> None:
    """Start the FastAPI REST API server using uvicorn."""
    try:
        import uvicorn
    except ImportError:
        print(
            "Error: uvicorn is required to run the API server.\n"
            "Install it with:  pip install 'uvicorn[standard]'",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Starting coursemap API server on http://{args.host}:{args.port}\n"
        f"  API docs:  http://{args.host}:{args.port}/docs\n"
        f"  Press Ctrl-C to stop.",
        file=sys.stderr,
    )
    uvicorn.run(
        "coursemap.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="coursemap",
        description="Degree planner for Massey University.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  coursemap majors --search psychology\n"
            "  coursemap plan --major 'Mental Health and Addiction' --start-year 2025\n"
            "  coursemap plan --major 'Computer Science' --completed 159101,160101\n"
            "  coursemap plan --major 'English' --max-credits 30\n"
            "  coursemap plan --major 'Ecology' --campus M --mode INT\n"
            "  coursemap validate\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- majors ---------------------------------------------------------------
    majors_p = sub.add_parser("majors", help="List available majors.")
    majors_p.add_argument(
        "--search", metavar="QUERY",
        help="Filter by name. Supports partial words (e.g. 'comp sci' matches 'Computer Science').",
    )

    # -- minors ---------------------------------------------------------------
    minors_p = sub.add_parser("minors", help="List available minors.")
    minors_p.add_argument("--search", metavar="QUERY",
                          help="Filter by minor name (partial match).")
    minors_p.add_argument("--quality", choices=["scraped", "inferred"],
                          help="Filter by data quality.")
    minors_p.set_defaults(func=_cmd_minors)

    # -- courses --------------------------------------------------------------
    courses_p = sub.add_parser("courses", help="Browse the course catalogue.")
    courses_p.add_argument("--search", metavar="QUERY", help="Filter by title.")
    courses_p.add_argument("--level", type=int, metavar="N",
                           help="Filter by level (100, 200, 300, ...).")
    courses_p.add_argument("--campus", default=None, metavar="CODE",
                           help="Show only courses at this campus (D, M, A, W).")
    courses_p.add_argument("--mode", default=None, metavar="CODE",
                           help="Show only courses in this delivery mode (DIS, INT, BLK).")
    courses_p.add_argument("--semester", metavar="SEM", default=None,
                           choices=["S1", "S2", "SS"],
                           help="Show only courses available in this semester (S1, S2, SS).")
    courses_p.add_argument("--show-inactive", action="store_true", default=False,
                           help="Include courses with no current offerings.")

    # -- plan -----------------------------------------------------------------
    plan_p = sub.add_parser("plan", help="Generate a degree plan.")
    plan_p.add_argument("--major", metavar="NAME",
                        help="Major name (partial match accepted).")
    plan_p.add_argument("--double-major", dest="double_major", metavar="NAME",
                        help="Second major name for a combined double-major plan.")
    plan_p.add_argument("--minor", dest="minor", metavar="NAME",
                        help="Add a minor's courses to preferred electives (partial name match).")
    plan_p.add_argument("--start-year", type=int, default=None, metavar="YEAR",
                        help="First year of study (default: current year).")
    plan_p.add_argument("--start-semester", dest="start_semester",
                        choices=["S1", "S2", "SS"], default=None, metavar="SEM",
                        help="Starting semester: S1, S2, or SS (default: current semester).")
    plan_p.add_argument("--max-credits", type=int, default=60, metavar="N",
                        help="Credit cap per semester (default: 60 = full time).")
    plan_p.add_argument("--max-per-semester", dest="max_per_semester", type=int, metavar="N",
                        help="Maximum number of courses per semester.")
    plan_p.add_argument("--campus", default="D", metavar="CODE",
                        help="Campus: D (distance), M (Manawatu), A (Auckland), W (Wellington).")
    plan_p.add_argument("--mode", default="DIS", metavar="CODE",
                        help="Delivery mode: DIS (distance), INT (internal), BLK (block).")
    plan_p.add_argument("--completed", metavar="CODES",
                        help="Comma-separated codes already completed.")
    plan_p.add_argument("--transfer-credits", dest="transfer_credits", type=int,
                        default=0, metavar="N",
                        help="Unspecified credit recognition from prior learning (e.g. 60).")
    plan_p.add_argument("--prefer", metavar="CODES",
                        help="Comma-separated elective course codes to prioritise.")
    plan_p.add_argument("--exclude", metavar="CODES",
                        help="Comma-separated course codes to never schedule (opt-out).")
    plan_p.add_argument("--output", metavar="FILE",
                        help="Output JSON path (default: plan.json).")
    plan_p.add_argument("--no-summer", dest="no_summer", action="store_true", default=True,
                        help="Skip Summer School (SS) semesters.")
    plan_p.add_argument("--auto-fill", dest="auto_fill", action="store_true", default=False,
                        help="Auto-select subject-area electives to fill the free-elective gap.")
    plan_p.add_argument("--explain", action="store_true", default=False,
                        help="Show why courses were excluded or skipped.")
    plan_p.add_argument("--format", dest="output_format", choices=["text", "html", "json", "ical"],
                        default="text", help="Output format (default: text). 'ical' exports a calendar file.")
    plan_p.add_argument("--debug", action="store_true", default=False,
                        help="Show scheduler diagnostics after plan generation.")

    # -- validate -------------------------------------------------------------
    validate_p = sub.add_parser("validate", help="Check dataset integrity.")
    validate_p.add_argument("--quiet", action="store_true", default=False,
                            help="Suppress warnings; only show errors.")
    validate_p.add_argument("--all", dest="all_warnings", action="store_true", default=False,
                            help="Show all warnings (default: first 20).")

    # -- serve ----------------------------------------------------------------
    dq_p = sub.add_parser("data-quality", help="Print a data quality report for the bundled datasets.")
    dq_p.add_argument("--json", dest="json_out", action="store_true", default=False,
                       help="Output as JSON instead of human-readable text.")

    serve_p = sub.add_parser("serve", help="Start the coursemap REST API server.")
    serve_p.add_argument("--host", default="127.0.0.1", metavar="HOST",
                         help="Bind host (default: 127.0.0.1). Use 0.0.0.0 to expose on LAN.")
    serve_p.add_argument("--port", type=int, default=8000, metavar="PORT",
                         help="Port to listen on (default: 8000).")
    serve_p.add_argument("--reload", action="store_true", default=False,
                         help="Auto-reload on code changes (development mode).")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if   args.command == "majors":   _cmd_majors(args)
    elif args.command == "minors":   _cmd_minors(args)
    elif args.command == "courses":  _cmd_courses(args)
    elif args.command == "plan":     _cmd_plan(args)
    elif args.command == "validate": _cmd_validate(args)
    elif args.command == "data-quality": _cmd_data_quality(args)
    elif args.command == "serve":        _cmd_serve(args)


if __name__ == "__main__":
    main()
