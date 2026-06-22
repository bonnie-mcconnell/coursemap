"""
Fee estimation for Massey University courses.

Based on Massey's published 2026 fee schedules.
Fees are per-credit estimates derived from typical course fees.
All figures are approximate - always check massey.ac.nz/fees for exact amounts.

Domestic fees source: https://www.massey.ac.nz/fees
International fees are approximately 2.5–3x domestic (varies by programme).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Domestic fee-per-credit by subject area (NZD, 2026 approximate)
# ---------------------------------------------------------------------------
# Based on Massey's published per-course fees divided by typical credit value.
# Undergraduate (L100-400) and postgraduate (L700-900) use different rates.
#
# Tier 1 (arts, humanities, social sciences): ~$50-55/cr
# Tier 2 (science, IT, business, applied):    ~$58-65/cr
# Tier 3 (health, engineering, professional): ~$65-80/cr
# Tier 4 (veterinary, medicine, creative):    ~$75-95/cr

_SUBJECT_FEE_PER_CREDIT: dict[str, float] = {
    # Arts & Humanities (Tier 1)
    "Art History":          52.0,
    "Classical Studies":    52.0,
    "Classics":             52.0,
    "Development Studies":  52.0,
    "Music":                58.0,
    "Philosophy":           52.0,
    "Politics":             52.0,
    "Sociology":            52.0,
    "International Relations": 52.0,
    "History":              52.0,
    "Geography":            55.0,

    # Social Science (Tier 1-2)
    "Criminology":          54.0,
    "Counselling":          58.0,
    "Psychology":           55.0,
    "Social Work":          58.0,

    # Business (Tier 2)
    "Accounting":           60.0,
    "Business":             60.0,
    "Business Administration": 62.0,
    "Business Law":         60.0,
    "Economics":            58.0,
    "Finance":              62.0,
    "Financial Planning":   60.0,
    "Human Resources":      60.0,
    "Management":           60.0,
    "Marketing":            60.0,
    "Strategic Studies":    60.0,

    # STEM (Tier 2)
    "Applied Statistics":   62.0,
    "Biomedical Science":   65.0,
    "Chemistry":            65.0,
    "Computer Science":     65.0,
    "Data Science":         65.0,
    "Environmental Science": 62.0,
    "Information Sciences": 63.0,
    "Information Systems":  62.0,
    "Information Technology": 62.0,
    "Mathematics":          62.0,
    "Physics":              65.0,
    "Software Engineering": 65.0,
    "Statistics":           62.0,

    # Agriculture & Environment (Tier 2-3)
    "Agriculture":          65.0,
    "Construction":         68.0,
    "Environmental Management": 62.0,

    # Health (Tier 3)
    "Defence Studies":      55.0,
    "Dietetics":            72.0,
    "Education":            58.0,
    "Health Science":       65.0,
    "Midwifery":            75.0,
    "Nursing":              72.0,
    "Paramedicine":         72.0,
    "Physiotherapy":        78.0,
    "Sport & Exercise Science": 65.0,
    "Sport Science":        65.0,

    # Design & Creative (Tier 3-4)
    "Design":               70.0,

    # Research/generic (fallback)
    "Research Methods":     58.0,
    "Foundation Studies":   50.0,
}

# Default fee per credit for unknown subject areas
_DEFAULT_FEE_PER_CREDIT_UG = 60.0   # undergraduate
_DEFAULT_FEE_PER_CREDIT_PG = 75.0   # postgraduate (L700+)

# International multiplier (applied on top of domestic)
# Massey international fees vary by programme but average ~2.8x domestic
_INTERNATIONAL_MULTIPLIER = 2.8


def fee_per_credit(subject_area: str | None, level: int, student_type: str = "domestic") -> float:
    """
    Return the estimated fee per credit for a given subject area and level.

    Args:
        subject_area: Course subject area string (may be None).
        level:        Course level (100, 200, 300, 400, 700, 800, 900).
        student_type: "domestic" or "international".

    Returns:
        Estimated fee in NZD per credit.
    """
    if subject_area and subject_area in _SUBJECT_FEE_PER_CREDIT:
        base = _SUBJECT_FEE_PER_CREDIT[subject_area]
    elif level >= 700:
        base = _DEFAULT_FEE_PER_CREDIT_PG
    else:
        base = _DEFAULT_FEE_PER_CREDIT_UG

    # Postgraduate surcharge (L700-900 cost more even in the same subject)
    if level >= 700 and base < 70:
        base = max(base, 70.0)

    if student_type == "international":
        base *= _INTERNATIONAL_MULTIPLIER

    return base


def estimate_course_fee(credits: int, subject_area: str | None, level: int,
                         student_type: str = "domestic") -> float:
    """Estimate total fee for a single course."""
    return credits * fee_per_credit(subject_area, level, student_type)


def estimate_plan_fees(
    semesters: list[dict],
    student_type: str = "domestic",
) -> dict:
    """
    Estimate total fees for a full degree plan.

    Args:
        semesters: List of semester dicts from the plan API response,
                   each with a 'courses' list of course dicts.
        student_type: "domestic" or "international".

    Returns:
        Dict with 'total', 'by_year', and 'disclaimer' keys.
    """
    total = 0.0
    by_year: dict[int, float] = {}

    for sem in semesters:
        year = sem.get("year", 0)
        year_total = 0.0
        for course in sem.get("courses", []):
            credits = course.get("credits", 15)
            level = course.get("level", 100)
            subject_area = course.get("subject_area")
            year_total += estimate_course_fee(credits, subject_area, level, student_type)
        by_year[year] = by_year.get(year, 0.0) + year_total
        total += year_total

    return {
        "total": round(total, -1),   # round to nearest $10
        "by_year": {yr: round(amt, -1) for yr, amt in sorted(by_year.items())},
        "student_type": student_type,
        "disclaimer": (
            "Fee estimates are based on approximate 2026 rates and are not guaranteed. "
            "Actual fees vary by course and change annually. "
            "Check massey.ac.nz/fees for exact amounts before enrolling."
        ),
    }
