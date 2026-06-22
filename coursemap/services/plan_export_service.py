"""
PlanExportService - formats a stored plan dict into various export shapes.

Extracted from the server.py route handlers so the formatting logic is
testable independently of the HTTP layer.
"""
from __future__ import annotations


class PlanExportService:
    """Converts stored plan dicts into human-readable export formats."""

    # ── Plain text (advisor summary) ─────────────────────────────────────────

    @staticmethod
    def to_advisor_text(cached: dict, plan_id: str = "") -> str:
        """
        Return a plain-text summary of a stored plan suitable for printing
        or emailing to an academic advisor.
        """
        meta      = cached.get("meta", {})
        semesters = cached.get("semesters", [])
        warnings  = cached.get("warnings", [])

        lines = [
            "COURSEMAP - UNOFFICIAL DEGREE PLAN SUMMARY",
            "=" * 52,
            f"Major:           {meta.get('major', '?')}",
            f"Campus / Mode:   {meta.get('campus', '?')} / {meta.get('mode', '?')}",
            f"Starting:        {meta.get('start_semester', '?')} {meta.get('start_year', '?')}",
            f"Degree target:   {meta.get('degree_target', 360)} credits",
            f"Credits planned: {meta.get('credits_total', 0)} credits",
        ]

        gap = meta.get("free_elective_gap", 0)
        if gap > 0:
            expl = meta.get("gap_explanation") or f"{gap}cr of free electives unscheduled."
            lines.append(f"Remaining gap:   {gap}cr - {expl}")

        lines += ["", "SEMESTER SCHEDULE", "-" * 52]

        for sem in semesters:
            lines.append(f"\n{sem['semester']} {sem['year']}  ({sem['credits']} credits)")
            for c in sem.get("courses", []):
                flag = "" if c.get("prereq_data_available") else "  ⚠ no prereq data"
                lines.append(f"  {c['code']}  {c['title'][:48]:<48}  {c['credits']}cr{flag}")

        if warnings:
            lines += ["", "WARNINGS", "-" * 52]
            for w in warnings:
                lines.append(f"  {w}")

        lines += [
            "",
            "=" * 52,
            "UNOFFICIAL - always verify with Massey University and your academic advisor.",
            "massey.ac.nz/study  |  coursemap (unofficial tool)",
        ]
        if plan_id:
            lines.append(f"Plan ID: {plan_id}")

        return "\n".join(lines)

    # ── Markdown (future use) ─────────────────────────────────────────────────

    @staticmethod
    def to_markdown(cached: dict) -> str:
        """Return a Markdown-formatted degree plan summary."""
        meta      = cached.get("meta", {})
        semesters = cached.get("semesters", [])
        warnings  = cached.get("warnings", [])

        lines = [
            f"# Degree Plan - {meta.get('major', 'Unknown')}",
            "",
            f"| Field | Value |",
            f"|---|---|",
            f"| Campus / Mode | {meta.get('campus', '?')} / {meta.get('mode', '?')} |",
            f"| Starting | {meta.get('start_semester', '?')} {meta.get('start_year', '?')} |",
            f"| Degree target | {meta.get('degree_target', 360)} credits |",
            f"| Credits planned | {meta.get('credits_total', 0)} credits |",
            "",
        ]

        gap = meta.get("free_elective_gap", 0)
        if gap > 0:
            lines.append(f"> **Free elective gap: {gap}cr** - {meta.get('gap_explanation', '')}")
            lines.append("")

        for sem in semesters:
            lines.append(f"## {sem['semester']} {sem['year']} ({sem['credits']}cr)")
            lines.append("")
            lines.append("| Code | Title | Credits | Notes |")
            lines.append("|---|---|---|---|")
            for c in sem.get("courses", []):
                note = "⚠ no prereq data" if not c.get("prereq_data_available") else ""
                lines.append(f"| {c['code']} | {c['title']} | {c['credits']} | {note} |")
            lines.append("")

        if warnings:
            lines.append("## Warnings")
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

        lines.append("*Unofficial - always verify with Massey University and your academic advisor.*")
        return "\n".join(lines)
