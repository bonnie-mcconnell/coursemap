"""
iCalendar export for degree plans.

Generates a .ics file with one multi-day event per semester so the plan
can be imported into Google Calendar, Apple Calendar, Outlook, etc.

Each semester block contains:
- SUMMARY: semester label and total credits
- DESCRIPTION: course list with codes, credits and titles
- DTSTART / DTEND: approximate real-world dates derived from Massey's
  published academic calendar (Semester 1 starts late February, Semester 2
  starts mid-July, Summer School starts late November).

The dates are representative only - Massey's exact enrolment dates vary
by year. Students should check massey.ac.nz for official dates.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from coursemap.domain.plan import DegreePlan


# ---------------------------------------------------------------------------
# Massey academic calendar approximations
# ---------------------------------------------------------------------------
# (month, day) for the start of each semester type, used as a baseline.
# "SS" = Summer School (straddling Nov/Dec of year N into Feb of year N+1).

_SEMESTER_START: dict[str, tuple[int, int]] = {
    "S1": (2, 24),   # late February
    "S2": (7, 14),   # mid-July
    "SS": (11, 25),  # late November
}

_SEMESTER_WEEKS: dict[str, int] = {
    "S1": 17,   # ~16 teaching weeks + study/exam period
    "S2": 17,
    "SS": 10,   # Summer School is shorter
}


def _semester_dates(year: int, sem: str) -> tuple[date, date]:
    """
    Return (start, end) dates for a given year and semester type.

    Summer School starts in late November of ``year`` and ends in early
    February of ``year + 1``.  The date constructor handles year-end rollover
    automatically via timedelta addition.
    """
    m, d = _SEMESTER_START[sem]
    start = date(year, m, d)
    end   = start + timedelta(weeks=_SEMESTER_WEEKS[sem])
    return start, end


def _escape_ical_text(value: str) -> str:
    """
    Escape a string for use in an iCalendar TEXT property value per RFC 5545 §3.3.11.

    The spec requires:
      - Backslashes escaped as \\\\
      - Semicolons escaped as \\;
      - Commas escaped as \\,
      - Literal newlines represented as \\n (the two-character sequence)

    Note: this function receives plain Python strings (with real \\n newline chars)
    and produces the correctly escaped iCal representation.  It must be applied
    BEFORE the line is passed to _fold().
    """
    value = value.replace("\\", "\\\\")   # must be first
    value = value.replace(";", "\\;")
    value = value.replace(",", "\\,")
    value = value.replace("\n", "\\n")    # real newline → RFC 5545 \\n sequence
    return value


def plan_to_ical(
    plan: DegreePlan,
    major_label: str,
    campus: str = "D",
    mode: str = "DIS",
    output_path: str | Path | None = None,
) -> str:
    """
    Convert a DegreePlan to iCalendar (.ics) format.

    Args:
        plan:         The degree plan to export.
        major_label:  Human-readable major name for event summaries.
        campus:       Campus code (informational only, included in description).
        mode:         Delivery mode code (informational only).
        output_path:  If given, write the .ics content to this file path.

    Returns:
        The raw .ics content as a string.
    """
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//coursemap//Massey Degree Planner//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _fold(f"X-WR-CALNAME:{_escape_ical_text(f'Degree Plan – {major_label}')}"),
        "X-WR-TIMEZONE:Pacific/Auckland",
    ]

    delivery = f"{campus}/{mode}"

    for i, semester in enumerate(plan.semesters):
        start, end = _semester_dates(semester.year, semester.semester)

        total_cr = semester.total_credits()

        # Build course list as a plain multi-line string, then escape as one unit
        course_lines = "\n".join(
            f"  {c.code}  {c.credits:2}cr  {c.title}"
            for c in semester.courses
        )
        raw_description = (
            f"Degree: {major_label}\n"
            f"Delivery: {delivery}\n"
            f"Credits this semester: {total_cr}cr\n"
            f"\nCourses:\n{course_lines}\n"
            f"\nDates are approximate. Check massey.ac.nz for official dates."
        )
        description = _escape_ical_text(raw_description)

        summary = f"{semester.year} {semester.semester} – {total_cr}cr ({major_label})"
        # Sanitise UID: keep only ASCII alphanumerics, hyphens, dots, @
        _label_safe = re.sub(r"[^A-Za-z0-9]", "-", major_label)
        _label_safe = re.sub(r"-+", "-", _label_safe).strip("-")[:40]
        uid = f"coursemap-{_label_safe}-{semester.year}-{semester.semester}-{i}@massey"

        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{date.today().strftime('%Y%m%dT000000Z')}",
            "SEQUENCE:0",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
            _fold(f"SUMMARY:{_escape_ical_text(summary)}"),
            _fold(f"DESCRIPTION:{description}"),
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines) + "\r\n"

    if output_path is not None:
        Path(output_path).write_text(content, encoding="utf-8")

    return content


def _fold(line: str, limit: int = 75) -> str:
    """
    Fold a long iCalendar content line per RFC 5545 §3.1.

    Lines longer than 75 octets are split with CRLF + single space continuation.
    """
    if len(line.encode("utf-8")) <= limit:
        return line
    result = []
    while len(line.encode("utf-8")) > limit:
        # Find safe split point (avoid splitting multi-byte chars)
        cut = limit
        while len(line[:cut].encode("utf-8")) > limit:
            cut -= 1
        result.append(line[:cut])
        line = " " + line[cut:]
    result.append(line)
    return "\r\n".join(result)
