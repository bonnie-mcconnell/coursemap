from __future__ import annotations
import hashlib
import json
import os
import re
import threading

import logging
from datetime import date as _date
from collections import deque
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from coursemap.domain.prerequisite import prereq_to_dict, prereq_to_human
from coursemap.domain.requirement_nodes import (
    AllOfRequirement, AnyOfRequirement, ChooseCreditsRequirement,
    ChooseNRequirement, CourseRequirement, MajorRequirement,
    MaxLevelCreditsRequirement, MinLevelCreditsFromRequirement,
    MinLevelCreditsRequirement, TotalCreditsRequirement,
)
from coursemap.ingestion.dataset_loader import load_courses, load_majors
from coursemap.ingestion.minor_loader import load_minors, search_minors
from coursemap.ingestion.freshness import freshness_report
from coursemap.services.planner_service import PlannerService
from coursemap.validation.dataset_validator import validate_dataset
from coursemap.export.ical import plan_to_ical
from coursemap.domain.fees import estimate_plan_fees, fee_per_credit, estimate_course_fee
from coursemap.api.plan_store import plan_store, async_get as _astore_get, async_put as _astore_put
from coursemap.services.plan_export_service import PlanExportService as _PlanExportSvc

logger = logging.getLogger(__name__)

_UI_HTML = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


# Bump when planner logic changes in a way that makes cached plans stale
_CACHE_VERSION = "v7.1"


def _plan_cache_key(req: "PlanRequest") -> str:
    """
    Return a stable 16-char hex key for a plan request (excludes seed field).

    Includes _CACHE_VERSION in the hashed payload so that any backend logic
    or response-shape change (a version bump) automatically invalidates all
    previously cached plans, rather than serving stale `meta` fields from
    before the change. This replaces the old approach of patching individual
    fields on cache-hit, which could silently drift out of sync with fresh
    generation logic (see _build_gap_meta).
    """
    d = req.model_dump()
    d.pop("seed", None)  # seed is metadata, not part of the plan identity
    d["_cache_version"] = _CACHE_VERSION
    payload = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-warm the dataset on startup so first request isn't slow."""
    _svc()
    yield

app = FastAPI(
    title="coursemap",
    description="Degree planner API for Massey University.",
    version="7.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> Response:
    """Return clean JSON on any unhandled exception instead of a 500 traceback."""
    if isinstance(exc, HTTPException):
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)
    import traceback as _tb
    logger.error("Unhandled %s on %s %s:\n%s",
                 type(exc).__name__, request.method, request.url.path, _tb.format_exc())
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc),
                 "path": str(request.url.path)},
    )


# ALLOWED_ORIGINS: comma-separated list of allowed origins for CORS.
# Defaults to "*" (open) for local dev. Set to your deployed domain in production.
# e.g.  ALLOWED_ORIGINS="https://coursemap.example.com"
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
_allowed_origins: list[str] | str = (
    [o.strip() for o in _raw_origins.split(",") if o.strip()]
    if _raw_origins != "*"
    else ["*"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ---------------------------------------------------------------------------
# Shared singletons - loaded once on first request
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _svc() -> PlannerService:
    """Load dataset and create PlannerService singleton (cached after first call)."""
    logger.info("Loading datasets…")
    courses = load_courses()
    majors  = load_majors()
    logger.info("Loaded %d courses, %d majors.", len(courses), len(majors))
    return PlannerService(courses, majors)


@lru_cache(maxsize=1)
def _courses() -> dict:
    return _svc().courses


@lru_cache(maxsize=1)
def _minors() -> list:
    return load_minors()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class PlanRequest(BaseModel):
    major: str = Field(..., description="Major name (partial match accepted).")
    double_major: str | None = Field(None, description="Second major for a combined plan.")
    start_year: int = Field(default_factory=lambda: _date.today().year, description="First calendar year of study.")
    start_semester: str = Field("S1", description="Starting semester: S1, S2, or SS.")
    max_credits: int = Field(60, ge=15, le=120, description="Credit cap per semester.")
    max_per_semester: int | None = Field(None, ge=1, le=10, description="Course count cap.")
    campus: str = Field("D", description="Campus code: D, M, A, W.")
    mode: str = Field("DIS", description="Delivery mode: DIS, INT, BLK.")
    completed: list[str] = Field(default_factory=list, description="Already-completed course codes.")
    transfer_credits: int = Field(0, ge=0, le=360, description="Prior-learning credit recognition (0–360cr).")
    prefer: list[str] = Field(default_factory=list, description="Elective codes to prioritise.")
    exclude: list[str] = Field(default_factory=list, description="Course codes to never schedule.")
    no_summer: bool = Field(True, description="Skip Summer School semesters.")
    auto_fill: bool = Field(False, description="Auto-fill free-elective gap with subject-area courses.")
    seed: int | None = Field(None, description="Random seed for deterministic plan generation. Returned in plan meta; use to reproduce a plan exactly.")


class SemesterOut(BaseModel):
    year: int
    semester: str
    credits: int
    courses: list[dict]


class PlanOut(BaseModel):
    plan_id: str = Field("", description="Stable ID for this plan. Use /api/plan/{plan_id} to retrieve it later.")
    meta: dict
    semesters: list[SemesterOut]
    warnings: list[str] = Field(default_factory=list)
    filler_codes: list[str] = Field(default_factory=list)
    double_major_info: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _course_to_dict(course) -> dict:
    # Distinct semesters this course runs in (any campus/mode)
    offered_sems = sorted(set(o.semester for o in course.offerings))
    return {
        "code":         course.code,
        "title":        course.title,
        "credits":      course.credits,
        "level":        course.level,
        "url":          course.url,
        "description":  course.description,
        "subject_area": getattr(course, "subject_area", None),
        "offered_semesters": offered_sems,
        "offerings": [
            {
                "semester":      o.semester,
                "campus":        o.campus,
                "mode":          o.mode,
                "location":      o.location,
            }
            for o in course.offerings
        ],
        "prerequisites":           prereq_to_dict(course.prerequisites),
        "prerequisites_human":     prereq_to_human(course.prerequisites),
        "prereq_data_available":   course.prerequisites is not None,
        "prerequisite_expression": prereq_to_dict(course.prerequisites),
        "restrictions":            list(course.restrictions or []),
        "corequisites":            list(course.corequisites or []),
        "offering_inferred":       getattr(course, "offering_inferred", False),
        "has_full_year_offering":  any(getattr(o, "full_year", False) for o in course.offerings),
    }


def _build_gap_meta(
    plan, svc: PlannerService, req: PlanRequest,
    resolved_name: str, double_info: dict | None,
) -> dict[str, Any]:
    """
    Compute the free-elective gap, degree target, and a human-readable
    explanation of why any remaining gap exists.

    Returns a dict with keys: degree_total, raw_gap, residual_gap, gap_explanation.
    """
    major_name = req.major
    gap          = svc.free_elective_gap(major_name, campus=req.campus, mode=req.mode)
    degree_total = svc.degree_total_credits(major_name)

    # raw_gap: the major's structural gap (what the major data alone requires),
    # net of any transfer credits already counted toward the degree.
    raw_gap = max(0, gap - plan.transfer_credits)

    # residual_gap: credits still unscheduled. This is 0 once the plan total
    # reaches the degree target, regardless of *how* it got filled (explicit
    # auto-fill, or PlanSearch's own elective selection during generation).
    credits_total = plan.total_credits() + plan.prior_credits() + plan.transfer_credits
    if req.auto_fill or credits_total >= degree_total:
        residual_gap = max(0, degree_total - credits_total)
    else:
        residual_gap = raw_gap

    gap_explanation: str | None = None
    if residual_gap > 0 and not req.auto_fill:
        if double_info:
            gap_explanation = (
                f"{residual_gap}cr of free electives remain after double major overlap. "
                "Enable Auto-fill to fill these automatically."
            )
        else:
            name_lc = resolved_name.lower()
            if "information sciences" in name_lc:
                gap_explanation = (
                    f"{residual_gap}cr of free electives are required. "
                    "BInfSc typically expects a second major to fill this gap - "
                    "add one below, or enable Auto-fill to select courses automatically."
                )
            elif "bachelor of science" in name_lc or "bachelor of arts" in name_lc:
                gap_explanation = (
                    f"{residual_gap}cr of free electives are required. "
                    "BSc/BA students typically add a second major or minor. "
                    "Enable Auto-fill or add a second major to complete your plan."
                )
            else:
                gap_explanation = (
                    f"{residual_gap}cr of free electives are unscheduled. "
                    "Enable Auto-fill to select courses automatically."
                )

    return {
        "degree_total":     degree_total,
        "raw_gap":          raw_gap,
        "residual_gap":     residual_gap,
        "gap_explanation":  gap_explanation,
    }


def _build_prereq_coverage(plan) -> dict[str, Any]:
    """Summarise prerequisite-data coverage across every scheduled course."""
    all_scheduled = [c for s in plan.semesters for c in s.courses]
    total   = len(all_scheduled)
    with_data = sum(1 for c in all_scheduled if c.prerequisites is not None)
    missing   = [c.code for c in all_scheduled if c.prerequisites is None and c.level >= 200]
    return {
        "total_courses":      total,
        "courses_with_data":  with_data,
        "coverage_pct":       round(100 * with_data / total) if total else 100,
        "missing_data_codes": missing[:20],  # cap at 20 for response size
    }


def _build_plan_warnings(
    plan, svc: PlannerService, req: PlanRequest, extra_warnings: list[str] | None,
) -> list[str]:
    """Collect every user-facing warning for a generated plan."""
    major_name = req.major
    warnings: list[str] = list(extra_warnings or [])

    # Required courses unavailable at the chosen campus/mode.
    try:
        all_required = svc.required_course_codes(major_name)
        scheduled    = {c.code for s in plan.semesters for c in s.courses}
        not_avail    = [
            code for code in all_required
            if code not in scheduled and code not in frozenset(req.completed or [])
        ]
        courses_map  = _courses()
        campus_unavail = [
            code for code in not_avail
            if code in courses_map
            and not any(
                o.campus == req.campus and o.mode == req.mode
                for o in courses_map[code].offerings
            )
        ]
        if campus_unavail:
            warnings.append(
                f"⚠ {len(campus_unavail)} required course(s) are not available at "
                f"{req.campus}/{req.mode} and were excluded from this plan: "
                f"{', '.join(sorted(campus_unavail)[:5])}"
                + (f" (and {len(campus_unavail)-5} more)" if len(campus_unavail) > 5 else "")
                + ". Try a different campus or delivery mode."
            )
    except Exception:
        pass  # non-fatal; best-effort warning only

    # Required courses the student explicitly excluded.
    if req.exclude:
        excl_req = svc.student_excluded_required_courses(
            major_name, frozenset(req.exclude), campus=req.campus, mode=req.mode
        )
        if excl_req:
            warnings.append(
                f"Excluded course(s) are required by this degree: {', '.join(excl_req)}. "
                "Major requirements will NOT be satisfied."
            )

    # Full Year courses - must enrol for all of S1+S2, not just one semester.
    full_year_codes = [
        c.code for s in plan.semesters for c in s.courses
        if any(getattr(o, "full_year", False) for o in c.offerings)
    ]
    if full_year_codes:
        warnings.append(
            f"⚠ {len(full_year_codes)} course(s) run the full academic year (S1+S2 combined enrolment): "
            + ", ".join(full_year_codes[:5])
            + (f" and {len(full_year_codes)-5} more" if len(full_year_codes) > 5 else "")
            + ". Verify enrolment requirements at massey.ac.nz before treating them as a single semester."
        )

    return warnings


def _plan_to_out(
    plan,
    svc: PlannerService,
    req: PlanRequest,
    filler_codes: list[str] | None = None,
    double_info: dict | None = None,
    extra_warnings: list[str] | None = None,
    plan_id: str = "",
) -> PlanOut:
    """
    Serialise a generated DegreePlan into the API's PlanOut response shape.

    Orchestrates four independent concerns, each delegated to a helper:
    gap/meta calculation, prerequisite coverage stats, warning collection,
    and double-major info formatting.
    """
    semesters_out = [
        SemesterOut(
            year=s.year,
            semester=s.semester,
            credits=s.total_credits(),
            courses=[_course_to_dict(c) for c in s.courses],
        )
        for s in plan.semesters
    ]

    filler = filler_codes or []

    try:
        resolved      = svc.resolve_major(req.major)
        resolved_name = resolved[0]["name"]
    except (ValueError, IndexError):
        resolved_name = req.major

    gap_info = _build_gap_meta(plan, svc, req, resolved_name, double_info)

    meta: dict[str, Any] = {
        "major":             resolved_name,
        "campus":            req.campus,
        "mode":              req.mode,
        "start_year":        req.start_year,
        "start_semester":    req.start_semester,
        "credits_planned":   plan.total_credits(),
        "credits_prior":     plan.prior_credits(),
        "credits_transfer":  plan.transfer_credits,
        "credits_total":     plan.total_credits() + plan.prior_credits() + plan.transfer_credits,
        "degree_target":     gap_info["degree_total"],
        "free_elective_gap": gap_info["residual_gap"],
        "raw_elective_gap":  gap_info["raw_gap"],
        "gap_explanation":   gap_info["gap_explanation"],
        "auto_filled_codes": filler if req.auto_fill else [],
        "prereq_coverage":   _build_prereq_coverage(plan),
    }

    warnings = _build_plan_warnings(plan, svc, req, extra_warnings)

    dmi_out: dict | None = None
    if double_info:
        dmi_out = {
            "first_label":   double_info["first_label"],
            "second_label":  double_info["second_label"],
            "shared_codes":  sorted(double_info["shared_codes"]),
            "saved_credits": double_info["saved_credits"],
            "first_gap":     double_info["first_gap"],
            "second_gap":    double_info["second_gap"],
        }
        meta["double_major"] = dmi_out

    return PlanOut(
        plan_id=plan_id,
        meta=meta,
        semesters=semesters_out,
        warnings=warnings,
        filler_codes=filler,
        double_major_info=dmi_out,
    )


def _execute_plan(req: PlanRequest, svc: PlannerService):
    """
    Single authoritative plan generation dispatch - used by both /api/plan
    and /api/plan/ical so flags only need updating in one place.
    Returns (plan, filler_codes, double_info).
    """
    # Normalise course codes: strip whitespace and uppercase
    prior     = frozenset(c.strip().upper() for c in req.completed if c.strip())
    preferred = frozenset(c.strip().upper() for c in req.prefer    if c.strip())
    excluded  = frozenset(c.strip().upper() for c in req.exclude   if c.strip())
    common    = dict(
        max_credits_per_semester  = req.max_credits,
        max_courses_per_semester  = req.max_per_semester,
        campus                    = req.campus,
        mode                      = req.mode,
        start_year                = req.start_year,
        start_semester            = req.start_semester,
        prior_completed           = prior,
        preferred_electives       = preferred,
        excluded_courses          = excluded,
        no_summer                 = req.no_summer,
        transfer_credits          = req.transfer_credits,
    )

    if req.double_major and req.auto_fill:
        plan, double_info, filler = svc.generate_filled_double_major_plan(
            major_name=req.major, second_major_name=req.double_major, **common
        )
        return plan, list(filler), double_info

    if req.double_major:
        plan, double_info = svc.generate_double_major_plan(
            major_name=req.major, second_major_name=req.double_major, **common
        )
        return plan, [], double_info

    if req.auto_fill:
        plan, filler = svc.generate_filled_plan(major_name=req.major, **common)
        return plan, list(filler), None

    plan = svc.generate_best_plan(major_name=req.major, **common)
    return plan, [], None



# ---------------------------------------------------------------------------
# Plan comparison endpoint
# ---------------------------------------------------------------------------

class CompareRequest(BaseModel):
    majors: list[str] = Field(..., min_length=2, max_length=4,
                               description="2–4 major names to compare side by side.")
    campus: str = Field("D")
    mode:   str = Field("DIS")
    start_year: int = Field(default_factory=lambda: _date.today().year)
    start_semester: str = Field("S1")
    max_credits: int = Field(60, ge=15, le=120)
    no_summer: bool = Field(True)
    completed: list[str] = Field(default_factory=list)


@app.post("/api/plan/compare", summary="Compare plans for 2–4 majors side by side")
@limiter.limit("10/minute")
def compare_plans(request: Request, req: CompareRequest):
    """
    Generate summary plans for 2–4 majors and return key metrics for each.

    Useful for students deciding between majors. Each entry reports semesters
    required, free elective gap, prerequisite data coverage, and how many
    courses overlap with the first major in the list.
    """
    svc = _svc()
    prior = frozenset(req.completed)
    results = []
    first_plan_codes: set[str] | None = None

    for major_name in req.majors:
        try:
            resolved = svc.resolve_major(major_name)
            canon = resolved[0]["name"]
            plan = svc.generate_best_plan(
                major_name=major_name,
                campus=req.campus, mode=req.mode,
                start_year=req.start_year, start_semester=req.start_semester,
                max_credits_per_semester=req.max_credits,
                no_summer=req.no_summer,
                prior_completed=prior,
            )
            plan_codes = {c.code for s in plan.semesters for c in s.courses}
            total_cr    = plan.total_credits() + plan.prior_credits()
            degree_tgt  = svc.degree_total_credits(major_name)
            gap         = svc.free_elective_gap(major_name, campus=req.campus, mode=req.mode)
            all_courses = [c for s in plan.semesters for c in s.courses]
            with_prereq = sum(1 for c in all_courses if c.prerequisites is not None)
            shared      = len(plan_codes & first_plan_codes) if first_plan_codes else 0
            if first_plan_codes is None:
                first_plan_codes = plan_codes
            results.append({
                "major": canon, "error": None,
                "semesters": len(plan.semesters),
                "credits_scheduled": total_cr,
                "degree_target": degree_tgt,
                "free_elective_gap": gap,
                "prereq_coverage_pct": round(100 * with_prereq / len(all_courses)) if all_courses else 100,
                "shared_with_first": shared,
            })
        except Exception as exc:
            results.append({"major": major_name, "error": str(exc)})

    return {"comparisons": results}


# ---------------------------------------------------------------------------
# UI route
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_ui():
    """Serve the web planner UI."""
    return HTMLResponse(content=_UI_HTML)


# ---------------------------------------------------------------------------
# API routes (all under /api prefix)
# ---------------------------------------------------------------------------

@app.get("/api", summary="API info and dataset status")
def api_root():
    fr = freshness_report()
    return {
        "name":    "coursemap",
        "version": "2.0.0",
        "description": "Massey University degree planner",
        "docs":    "/docs",
        "dataset": fr,
        "plan_store": plan_store.stats(),
    }


@app.get("/api/plan-store/stats", summary="Plan store statistics")
def plan_store_stats():
    """Return statistics about the persistent plan store."""
    return plan_store.stats()


@app.get("/api/freshness", summary="Dataset age and staleness report")
def get_freshness():
    return freshness_report()


@app.get("/api/data-quality", summary="Dataset quality report")
def data_quality_report():
    """
    Return a structured report on prerequisite data coverage and freshness.
    Useful for surfacing data gaps in the UI and for CI health checks.
    """
    import json as _json
    from collections import Counter as _Counter
    from coursemap.ingestion.dataset_loader import DATASET_PATH

    # Raw format distribution
    with open(DATASET_PATH, encoding="utf-8") as f:
        raw_courses = _json.load(f)

    fmt: dict[str, int] = _Counter()
    for c in raw_courses:
        pval = c.get("prerequisites")
        if pval is None:
            fmt["null"] += 1
        elif isinstance(pval, list):
            fmt["flat_list"] += 1
        elif isinstance(pval, dict):
            fmt["and_or_tree"] += 1
        elif isinstance(pval, str):
            fmt["single_code"] += 1

    total   = len(raw_courses)
    new_fmt = fmt.get("and_or_tree", 0) + fmt.get("single_code", 0)
    old_fmt = fmt.get("flat_list", 0)

    courses = _svc().courses
    with_prereqs = sum(1 for c in courses.values() if c.prerequisites is not None)

    freshness = freshness_report()

    return {
        "total_courses":          total,
        "prerequisite_formats":   dict(fmt),
        "structured_pct":         round(100 * new_fmt / total) if total else 0,
        "courses_with_prereqs_after_filter": with_prereqs,
        "courses_null_after_filter":         total - with_prereqs,
        "needs_rescrape":         old_fmt + fmt.get("null", 0),
        "freshness":              freshness,
        "recommendation":         (
            "Run `python -m coursemap.ingestion.refresh_prerequisites` to upgrade "
            f"{old_fmt + fmt.get('null', 0)} courses to structured AND/OR prerequisites."
        ) if (old_fmt + fmt.get("null", 0)) > 0 else "All courses use structured prerequisite format.",
    }




@app.get("/api/health", summary="Health check", tags=["meta"])
def health_check():
    """Returns service health status and data freshness."""
    try:
        svc = _svc()
        courses = _courses()
        fr = freshness_report()
        return {
            "status": "ok",
            "courses_loaded": len(courses),
            "majors_loaded": len(svc.majors),
            "dataset": fr,
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(exc)})

_specs_cache_store: dict = {}  # populated lazily on first /api/majors call

@app.get("/api/majors", summary="List or search majors")
@limiter.limit("200/minute")
def list_majors(
    request: Request,
    search: str | None = Query(None, description="Partial name search query."),
    limit:  int        = Query(50, ge=1, le=500, description="Max results."),
):
    svc = _svc()
    majors = svc.majors

    if search:
        words = search.strip().lower().split()
        def word_match(name: str) -> bool:
            tokens = name.lower().replace("–", " ").split()
            return all(any(w in tok for tok in tokens) for w in words)
        majors = [m for m in majors if word_match(m["name"])]

    majors_all = sorted(majors, key=lambda m: m["name"])
    total_count = len(majors_all)
    majors = majors_all[:limit]

    def _parse_qual(name: str) -> str:
        """Extract qualification type from major name: 'CS – Bachelor of Science' → 'Bachelor of Science'."""
        parts = re.split(r"\s+[\u2013\u2014-]\s+", name, maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    # Load specialisation metadata for richer major info
    if not _specs_cache_store:
        import json as _json
        from pathlib import Path as _Path
        _specs_path = _Path(__file__).resolve().parents[2] / "datasets" / "specialisations.json"
        if _specs_path.exists():
            for _s in _json.loads(_specs_path.read_text(encoding="utf-8")):
                _specs_cache_store[_s["title"]] = _s
    _specs_cache = _specs_cache_store

    def _enrich(m: dict) -> dict:
        name = m["name"]
        spec = _specs_cache.get(name, {})
        return {
            "name":               name,
            "url":                m.get("url", ""),
            "qualification_type": _parse_qual(name),
            "qual_code":          spec.get("qual_code", ""),
            "level":              spec.get("level", 7),
            "length_years":       spec.get("length", 3),
            "credit_target":      spec.get("length", 3) * 120,
        }

    return {
        "total":  total_count,
        "count":  len(majors),
        "majors": [_enrich(m) for m in majors],
    }


@app.get("/api/majors/resolve", summary="Resolve a major name to a canonical match")
def resolve_major(name: str = Query(..., description="Major name (partial match accepted).")):
    svc = _svc()
    try:
        resolved = svc.resolve_major(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "count":  len(resolved),
        "majors": [{"name": m["name"], "url": m.get("url", "")} for m in resolved],
    }


@app.get("/api/minors", summary="List or search minors")
def list_minors(
    search: str | None = Query(None, description="Search by minor name."),
    data_quality: str | None = Query(None, description="Filter: 'scraped' or 'inferred'."),
):
    """Return available minors. Scraped entries have real Massey data; inferred are approximated."""
    all_minors = _minors()
    minors = all_minors
    if search:
        minors = search_minors(search, minors)
    if data_quality:
        minors = [m for m in minors if m.get("data_quality") == data_quality]
    return {
        "count": len(minors),
        "total": len(all_minors),
        "scraped": sum(1 for m in all_minors if m.get("data_quality") == "scraped"),
        "inferred": sum(1 for m in all_minors if m.get("data_quality") == "inferred"),
        "minors": minors,
        "note": "Inferred minors are approximated from course patterns. Verify at massey.ac.nz/study/minors/",
    }


@app.get("/api/minors/{name}", summary="Get a single minor by name")
def get_minor(name: str):
    """Return a single minor's details and requirement tree."""
    minors = _minors()
    matches = [m for m in minors if m["name"].lower() == name.lower()]
    if not matches:
        matches = [m for m in minors if name.lower() in m["name"].lower()]
    if not matches:
        raise HTTPException(status_code=404, detail=f"Minor '{name}' not found.")
    return matches[0]


@app.get("/api/courses", summary="Browse course catalogue")
def list_courses(
    search:           str | None = Query(None, description="Search by title or course code (keywords). Alias: q."),
    q:                str | None = Query(None, description="Alias for search."),
    level:            int | None = Query(None, description="Filter by level (100, 200, 300, 400, 700, 800, 900)."),
    campus:           str | None = Query(None, description="Filter by campus code (D, M, A, W)."),
    mode:             str | None = Query(None, description="Filter by delivery mode (DIS, INT, BLK)."),
    subject_area:     str | None = Query(None, description="Filter by subject area (e.g. 'Computer Science', 'Psychology')."),
    exclude_research: bool       = Query(False, description="Exclude thesis/research/dissertation courses."),
    semester:         str | None = Query(None, description="Filter by semester availability (S1, S2, SS)."),
    min_credits:      int        = Query(0, ge=0, description="Minimum credit value (use 1 to exclude zero-credit courses)."),
    limit:            int        = Query(150, ge=1, le=3000, description="Max results to return."),
):
    # Allow ?q= as alias for ?search=
    if q and not search:
        search = q

    courses = list(_courses().values())

    # Always exclude zero-credit admin entries and level 900+ doctoral unless explicitly filtered
    if level is None:
        courses = [c for c in courses if c.credits > 0 and c.level < 900]

    if search:
        words = search.strip().lower().split()
        def _matches(c) -> bool:
            haystack = f"{c.code} {c.title}".lower()
            return all(w in haystack for w in words)
        courses = [c for c in courses if _matches(c)]

    if level is not None:
        # Allow 700 to match 700–799, etc.
        if level >= 100:
            courses = [c for c in courses if (c.level // 100) * 100 == (level // 100) * 100]
        else:
            courses = [c for c in courses if c.level == level]
    
    # Filter by subject area if specified
    if subject_area:
        courses = [c for c in courses if getattr(c, "subject_area", None) == subject_area
                   or subject_area.lower() in (getattr(c, "subject_area", "") or "").lower()]

    # Exclude thesis/research courses if requested (cleaner browse experience)
    if exclude_research:
        _research_kw = ('thesis', 'research', 'dissertation', 'phd', 'mphil', 'capstone project')
        courses = [
            c for c in courses
            if not any(kw in c.title.lower() for kw in _research_kw)
        ]

    if campus:
        courses = [c for c in courses if any(
            o.campus == campus
            for o in c.offerings
        )]

    if mode:
        courses = [c for c in courses if any(
            o.mode == mode
            for o in c.offerings
        )]

    if semester:
        sem_upper = semester.upper()
        courses = [c for c in courses if any(
            o.semester == sem_upper
            for o in c.offerings
        )]

    if min_credits > 0:
        courses = [c for c in courses if c.credits >= min_credits]

    # Only return courses with at least one offering so the modal is useful
    courses = [c for c in courses if c.offerings]
    courses.sort(key=lambda c: (c.level, c.code))

    total = len(courses)
    return {
        "count":   total,
        "showing": min(total, limit),
        "courses": [_course_to_dict(c) for c in courses[:limit]],
    }


@app.get("/api/courses/{code}", summary="Single course detail")
def get_course(code: str):
    courses = _courses()
    code = code.upper()
    if code not in courses:
        raise HTTPException(status_code=404, detail=f"Course '{code}' not found.")
    result = _course_to_dict(courses[code])
    if courses[code].credits == 0:
        result["_note"] = (
            "Zero-credit course (practicum/placement). "
            "Cannot be auto-scheduled and does not count toward degree credit totals."
        )
    return result


@app.get("/api/courses/{code}/prereq-chain", summary="Full prerequisite chain for a course")
def get_prereq_chain(code: str):
    """
    Return the full transitive prerequisite chain for a course as a DAG suitable
    for rendering. Each node in `nodes` has {code, title, credits, level}.
    Each edge in `edges` is {from, to}. `depth` gives the longest chain length
    (minimum semesters before this course can be taken).
    """
    courses = _courses()
    code = code.upper()
    if code not in courses:
        raise HTTPException(status_code=404, detail=f"Course '{code}' not found.")

    from coursemap.domain.prerequisite import CoursePrerequisite, AndExpression, OrExpression

    def direct_prereqs(c_code: str) -> set[str]:
        course = courses.get(c_code)
        if not course or not course.prerequisites:
            return set()
        result: set[str] = set()
        stack = [course.prerequisites]
        while stack:
            node = stack.pop()
            if isinstance(node, CoursePrerequisite):
                if node.code in courses:
                    result.add(node.code)
            elif isinstance(node, (AndExpression, OrExpression)):
                stack.extend(node.children)
        return result

    # BFS using deque (O(1) popleft vs O(n) pop(0))
    visited: set[str] = set()
    edges: list[dict] = []
    queue: deque[str] = deque([code])
    while queue:
        cur = queue.popleft()
        if cur in visited:
            continue
        visited.add(cur)
        for dep in direct_prereqs(cur):
            edges.append({"from": dep, "to": cur})
            if dep not in visited:
                queue.append(dep)

    # Compute depth (longest path from any root to each node) via relaxation
    depths: dict[str, int] = {n: 0 for n in visited}
    changed = True
    while changed:
        changed = False
        for e in edges:
            new_d = depths[e["from"]] + 1
            if new_d > depths[e["to"]]:
                depths[e["to"]] = new_d
                changed = True

    nodes = [
        {
            "code":    c,
            "title":   courses[c].title if c in courses else c,
            "credits": courses[c].credits if c in courses else 0,
            "level":   courses[c].level if c in courses else 0,
            "depth":   depths.get(c, 0),
        }
        for c in visited
    ]
    nodes.sort(key=lambda n: n["depth"])

    return {
        "code":        code,
        "nodes":       nodes,
        "edges":       edges,
        "chain_depth": depths.get(code, 0),
    }


@app.get("/api/courses/{code}/explain", summary="Explain why a course appears where it does in a plan")
def explain_course(
    code: str,
    major: str = Query(..., description="Major name."),
    campus: str = Query("D"),
    mode:   str = Query("DIS"),
):
    """
    Return a human-readable explanation of scheduling constraints for a course:
    - Which prerequisites it needs and when they become available
    - What offering semesters are available for this campus/mode
    - How deep its prerequisite chain is
    """
    courses = _courses()
    code = code.upper()
    if code not in courses:
        raise HTTPException(status_code=404, detail=f"Course '{code}' not found.")

    course = courses[code]

    # Offerings at requested campus/mode
    matching_offerings = [
        {"semester": o.semester, "campus": o.campus, "mode": o.mode}
        for o in course.offerings
        if o.campus == campus and o.mode == mode
    ]
    all_offerings = [
        {"semester": o.semester, "campus": o.campus, "mode": o.mode}
        for o in course.offerings
    ]

    # Get prereq chain depth
    chain_data = get_prereq_chain(code)
    chain_depth = chain_data["chain_depth"]

    # Build constraint summary
    constraints: list[str] = []

    if not course.offerings:
        constraints.append("This course has no recorded offerings - it may be discontinued or offered by arrangement only.")
    elif not matching_offerings:
        avail = sorted({f"{o['campus']}/{o['mode']}" for o in all_offerings})
        constraints.append(
            f"Not offered at {campus}/{mode}. Available at: {', '.join(avail)}."
        )
    else:
        sems = sorted({o["semester"] for o in matching_offerings})
        constraints.append(f"Offered at {campus}/{mode} in: {', '.join(sems)}.")

    if chain_depth == 0:
        if matching_offerings:
            sems_avail = sorted({o["semester"] for o in matching_offerings})
            first_sem = sems_avail[0]  # S1 < S2 < SS alphabetically by convention
            constraints.append(f"No prerequisites - can be taken in {first_sem} of year 1.")
        else:
            constraints.append("No prerequisites - can be taken in semester 1 (subject to offering availability).")
    else:
        # Compute a more accurate earliest-semester estimate that accounts for
        # offering constraints. Each semester slot is S1→S2→SS repeating.
        # The chain_depth tells us the minimum number of prior semesters needed.
        # If the course is only offered in one semester type, it may need to wait
        # an extra slot after prerequisites are met.
        sem_cycle = ["S1", "S2", "SS"]
        # Simulate: after chain_depth slots of prerequisites, what slot can this course land in?
        prereq_finish_slot = chain_depth - 1   # 0-indexed slot where last prereq finishes
        earliest_take_slot = chain_depth        # earliest slot to take this course (0-indexed)
        if matching_offerings:
            offered_sems = {o["semester"] for o in matching_offerings}
            # Walk forward from earliest_take_slot until we find a slot with a matching semester
            for offset in range(6):  # max 6 extra slots to find a matching semester
                candidate_sem = sem_cycle[(earliest_take_slot + offset) % 3]
                if candidate_sem in offered_sems:
                    earliest_take_slot += offset
                    break
        # Convert 0-indexed slot to human semester number (slot 0 = "semester 1")
        earliest_sem_number = earliest_take_slot + 1
        constraints.append(
            f"Prerequisite chain is {chain_depth} semester{'s' if chain_depth != 1 else ''} deep - "
            f"earliest possible semester: {earliest_sem_number}."
        )

    if course.prerequisites:
        prereq_str = prereq_to_human(course.prerequisites)
        constraints.append(f"Requires: {prereq_str}.")

    return {
        "code":               code,
        "title":              course.title,
        "credits":            course.credits,
        "level":              course.level,
        "offerings_matching": matching_offerings,
        "offerings_all":      all_offerings,
        "prerequisites_human": prereq_to_human(course.prerequisites),
        "chain_depth":        chain_depth,
        "constraints":        constraints,
    }


@app.post("/api/plan", response_model=PlanOut, summary="Generate a degree plan")
@limiter.limit("30/minute")
def generate_plan(request: Request, req: PlanRequest):
    svc = _svc()

    # Warn about unknown codes in user-supplied lists (before planning).
    catalogue = _courses()
    unknown_warnings: list[str] = []
    for label, codes in [("completed", req.completed), ("prefer", req.prefer), ("exclude", req.exclude)]:
        unknown = [c for c in codes if c not in catalogue]
        if unknown:
            unknown_warnings.append(
                f"Unknown course code(s) in '{label}': {', '.join(unknown)}. These will be ignored."
            )

    # Compute cache key - deterministic share ID
    plan_key = _plan_cache_key(req)

    # Return cached plan if available (makes shared links deterministic).
    # _CACHE_VERSION is baked into plan_key, so a stale cached entry from
    # before a logic change simply misses here and falls through to a fresh
    # generation below - no patch-on-read needed.
    cached = plan_store.get(plan_key)
    if cached is not None:
        return PlanOut(**cached)

    try:
        plan, filler, double_info = _execute_plan(req, svc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    result = _plan_to_out(
        plan, svc, req,
        filler_codes=filler,
        double_info=double_info,
        extra_warnings=unknown_warnings,
        plan_id=plan_key,
    )
    # Persist to SQLite for future retrieval via plan_id
    plan_store.put(plan_key, req.model_dump(), result.model_dump())
    return result


@app.get("/api/plan/{plan_id}", response_model=PlanOut, summary="Retrieve a previously generated plan by ID")
def get_plan(plan_id: str):
    """
    Retrieve a cached plan by its plan_id.
    plan_id values are returned in POST /api/plan responses and encoded in share links.
    Plans are held in memory - they survive server restarts only if the same parameters
    are re-submitted (which will regenerate and re-cache the same plan).
    """
    cached = plan_store.get(plan_id)
    if cached is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Plan not found. Plans persist across restarts. "
                "If this is an old link, re-submit the original parameters."
            ),
        )
    return PlanOut(**cached)


@app.post("/api/plan/stream", summary="Generate a plan with SSE progress events")
@limiter.limit("30/minute")
def generate_plan_stream(request: Request, req: PlanRequest):
    """
    Server-Sent Events (SSE) version of POST /api/plan.

    Emits a sequence of progress events so the UI can show a progress bar
    rather than a blank spinner. Events are newline-delimited JSON lines
    prefixed with 'data: '.

    Event types:
      {"type": "progress", "step": "resolving",   "pct": 10, "msg": "Resolving major…"}
      {"type": "progress", "step": "generating",  "pct": 40, "msg": "Generating plan…"}
      {"type": "progress", "step": "filling",     "pct": 70, "msg": "Filling electives…"}
      {"type": "progress", "step": "caching",     "pct": 90, "msg": "Saving plan…"}
      {"type": "done",     "plan_id": "…",  "plan": { full PlanOut dict }}
      {"type": "error",    "detail": "…"}

    The 'done' event contains the complete plan - clients should use that
    rather than making a second request.
    """
    import json as _json

    svc = _svc()
    catalogue = _courses()

    unknown_warnings: list[str] = []
    for label, codes in [("completed", req.completed), ("prefer", req.prefer), ("exclude", req.exclude)]:
        unknown = [c for c in codes if c not in catalogue]
        if unknown:
            unknown_warnings.append(
                f"Unknown course code(s) in '{label}': {', '.join(unknown)}. These will be ignored."
            )

    plan_key = _plan_cache_key(req)

    def _event(data: dict) -> str:
        return f"data: {_json.dumps(data)}\n\n"

    def _stream():
        # Step 1 - check cache
        cached = plan_store.get(plan_key)
        if cached is not None:
            yield _event({"type": "progress", "step": "cached", "pct": 95, "msg": "Loading saved plan…"})
            yield _event({"type": "done", "plan_id": plan_key, "plan": cached})
            return

        yield _event({"type": "progress", "step": "resolving", "pct": 10, "msg": "Resolving major…"})

        try:
            yield _event({"type": "progress", "step": "generating", "pct": 35, "msg": "Building requirement tree…"})

            # Double major takes longer - hint to the UI
            if req.double_major:
                yield _event({"type": "progress", "step": "generating", "pct": 50, "msg": "Scheduling double major…"})

            import threading, queue as _queue
            _result_q: _queue.Queue = _queue.Queue()

            def _run_plan():
                try:
                    r = _execute_plan(req, svc)
                    _result_q.put(("ok", r))
                except Exception as e:
                    _result_q.put(("err", e))

            _t = threading.Thread(target=_run_plan, daemon=True)
            _t.start()
            _t.join(timeout=60)  # 60s hard ceiling

            if _t.is_alive():
                yield _event({
                    "type": "error",
                    "detail": (
                        "Plan generation timed out (60s). "
                        "Try reducing the credit load, adding completed courses, "
                        "or choosing a simpler major combination."
                    )
                })
                return

            _status, _payload = _result_q.get_nowait()
            if _status == "err":
                raise _payload
            plan, filler, double_info = _payload

        except ValueError as exc:
            yield _event({"type": "error", "detail": str(exc)})
            return

        if req.auto_fill and filler:
            yield _event({"type": "progress", "step": "filling", "pct": 75, "msg": f"Added {len(filler)} elective(s)…"})
        else:
            yield _event({"type": "progress", "step": "filling", "pct": 75, "msg": "Finalising plan…"})

        yield _event({"type": "progress", "step": "caching", "pct": 90, "msg": "Saving plan…"})

        result = _plan_to_out(
            plan, svc, req,
            filler_codes=filler,
            double_info=double_info,
            extra_warnings=unknown_warnings,
            plan_id=plan_key,
        )
        plan_store.put(plan_key, req.model_dump(), result.model_dump())

        yield _event({"type": "done", "plan_id": plan_key, "plan": result.model_dump()})

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx buffering
        },
    )


@app.post("/api/plan/ical", summary="Generate a degree plan and return an .ics calendar file")
@limiter.limit("10/minute")
def generate_plan_ical(request: Request, req: PlanRequest):
    svc = _svc()

    # Use the plan cache so the iCal always matches what /api/plan would return
    # for the same request - important for shared plan links.
    plan_key = _plan_cache_key(req)
    cached = plan_store.get(plan_key)

    if cached is not None:
        # Reconstruct DegreePlan from cached JSON for the iCal exporter
        from coursemap.domain.plan import DegreePlan, SemesterPlan
        from coursemap.domain.course import Course, Offering
        cached_data = cached if isinstance(cached, dict) else json.loads(cached)
        try:
            sems = []
            for s in cached_data.get("semesters", []):
                course_objs = []
                for c in s.get("courses", []):
                    full = svc.courses.get(c["code"])
                    if full:
                        course_objs.append(full)
                sems.append(SemesterPlan(
                    year=s["year"],
                    semester=s["semester"],
                    courses=tuple(course_objs),
                ))
            plan = DegreePlan(semesters=tuple(sems))
            double_info = cached_data.get("double_major_info")
        except Exception:
            # Fall through to fresh generation if reconstruction fails
            cached = None

    if cached is None:
        try:
            plan, _, double_info = _execute_plan(req, svc)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Resolve label for calendar name
    if double_info:
        major_label = f"{double_info['first_label']} + {double_info['second_label']}"
    else:
        try:
            major_label = svc.resolve_major(req.major)[0]["name"]
        except (ValueError, IndexError):
            major_label = req.major

    ics_content = plan_to_ical(plan, major_label, campus=req.campus, mode=req.mode)
    import re as _re
    safe_name = _re.sub(r'[^\w\s-]', '', req.major.replace('–', '-').replace('-', '-'))
    filename = f"degree_plan_{safe_name.replace(' ', '_')[:40]}.ics"
    return Response(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



@app.post("/api/plan/fees", summary="Estimate fees for a plan")
def estimate_fees_for_plan(body: dict, student_type: str = Query("domestic", description="domestic or international")):
    """
    Estimate total fees for a plan given a list of semesters.
    Pass the `semesters` array from a /api/plan response.
    """
    from coursemap.domain.fees import estimate_plan_fees
    semesters = body.get("semesters", [])
    if not semesters:
        raise HTTPException(status_code=422, detail="No semesters provided.")
    return estimate_plan_fees(semesters, student_type=student_type)


@app.get("/api/fees/rate", summary="Fee per credit for a subject area")
def get_fee_rate(
    subject_area: str | None = Query(None),
    level: int = Query(100),
    student_type: str = Query("domestic"),
):
    """Return the estimated fee per credit for a subject area at a given level."""
    return {
        "subject_area": subject_area,
        "level": level,
        "student_type": student_type,
        "fee_per_credit": fee_per_credit(subject_area, level, student_type),
    }



# ---------------------------------------------------------------------------
# Mid-degree progress check
# ---------------------------------------------------------------------------

class ProgressRequest(BaseModel):
    major:     str
    completed: list[str] = Field(default_factory=list, description="Completed course codes.")
    campus:    str = Field("D")
    mode:      str = Field("DIS")


@app.post("/api/plan/progress", summary="Check mid-degree progress toward requirements")
@limiter.limit("30/minute")
def check_progress(request: Request, req: ProgressRequest):
    """
    Given a list of completed course codes, report progress toward degree completion.

    Returns:
    - credits_completed: credits earned so far
    - degree_target: total credits required
    - pct_complete: percentage of degree completed
    - required_met: list of required courses already done
    - required_remaining: required courses still needed
    - gap_remaining: free elective credits still unscheduled
    - on_track: True if the student's level progression looks reasonable
      (i.e. not trying to take L300 courses in year 1)
    """
    svc = _svc()
    courses_map = _courses()
    completed_set = frozenset(c.strip().upper() for c in req.completed if c.strip())

    # Credits earned
    credits_earned = sum(
        courses_map[c].credits for c in completed_set
        if c in courses_map and courses_map[c].credits > 0
    )

    # Degree target and required codes
    try:
        degree_target = svc.degree_total_credits(req.major)
        required_codes = svc.required_course_codes(req.major)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    required_met       = sorted(required_codes & completed_set)
    required_remaining = sorted(required_codes - completed_set)
    pct_complete       = round(100 * credits_earned / degree_target) if degree_target else 0

    # Free elective gap after completed credits
    raw_gap    = svc.free_elective_gap(req.major, campus=req.campus, mode=req.mode)
    remaining_credits = max(0, degree_target - credits_earned)

    # Level progression check: warn if student has completed < 60cr but has L300+ courses
    completed_levels = [courses_map[c].level for c in req.completed if c in courses_map]
    advanced_without_base = (
        any(lvl >= 300 for lvl in completed_levels)
        and credits_earned < 60
    )

    # Estimated semesters remaining.
    # Standard Massey load: 60cr/year across 2 semesters = 30cr/semester.
    # We use 30cr per active semester as the baseline, but cap at 1 semester
    # minimum when any credits remain, so the estimate is never misleadingly 0.
    sems_remaining = max(1, -(-remaining_credits // 30)) if remaining_credits > 0 else 0

    return {
        "major": req.major,
        "credits_completed": credits_earned,
        "degree_target": degree_target,
        "pct_complete": pct_complete,
        "required_met": required_met,
        "required_remaining": required_remaining,
        "required_total": len(required_codes),
        "required_done_count": len(required_met),
        "credits_remaining": remaining_credits,
        "gap_remaining": max(0, raw_gap - credits_earned),
        "estimated_semesters_remaining": sems_remaining,
        "on_track": not advanced_without_base,
        "warnings": (
            ["⚠ L300+ courses completed before 60cr base - check programme regulations."]
            if advanced_without_base else []
        ),
    }


@app.post("/api/reload", summary="Hot-reload datasets without restarting", tags=["meta"])
def reload_datasets():
    """
    Clear the in-process dataset cache and reload from disk.
    Call this after running the prerequisite scraper or any data refresh script.
    """
    _svc.cache_clear()
    _courses.cache_clear()
    _minors.cache_clear()
    _specs_cache_store.clear()
    # plan_store cache is version-keyed - old plans auto-invalidated on next fetch
    svc = _svc()  # pre-warm
    return {
        "status": "reloaded",
        "courses_loaded": len(svc.courses),
        "majors_loaded": len(svc.majors),
    }


@app.get("/api/validate", summary="Dataset integrity report")
def validate():
    svc = _svc()
    result = validate_dataset(svc.courses, svc.majors, raise_on_error=False)
    return {
        "errors":        result.errors,
        "warnings":      result.warnings[:50],
        "error_count":   len(result.errors),
        "warning_count": len(result.warnings),
        "passed":        len(result.errors) == 0,
    }


class ValidateRequest(BaseModel):
    major: str = Field(..., description="Major name to validate against.")
    double_major: str | None = Field(None, description="Second major, for validating a double-major plan against both requirement trees.")
    plan_id: str | None = Field(None, description="plan_id from a previously generated plan. Takes precedence over course_codes.")
    course_codes: list[str] = Field(default_factory=list, description="Ordered list of course codes in the plan (semester order). Used when plan_id is absent.")
    completed: list[str] = Field(default_factory=list, description="Already-completed course codes.")
    transfer_credits: int = Field(0, ge=0)


def _validation_node_to_check(node, courses: dict, all_codes: set[str], grand_total: int, claimed: set[str]) -> dict:
    """
    Recursively convert a requirement node to a UI-renderable checklist item.

    `claimed` is the set of course codes already named by some specific
    requirement elsewhere in the full tree being checked - used so an open
    free-elective pool doesn't double-count a course that's already
    satisfying a more specific requirement.
    """
    if isinstance(node, CourseRequirement):
        c = courses.get(node.course_code)
        passed = node.course_code in all_codes
        return {
            "type": "course",
            "code": node.course_code,
            "title": c.title if c else node.course_code,
            "credits": c.credits if c else 0,
            "passed": passed,
            "label": f"{node.course_code} - {c.title if c else '?'}",
        }

    if isinstance(node, TotalCreditsRequirement):
        return {
            "type": "total_credits",
            "required": node.required_credits,
            "actual": grand_total,
            "passed": grand_total >= node.required_credits,
            "label": f"Total credits: {grand_total} / {node.required_credits}cr required",
        }

    if isinstance(node, MinLevelCreditsRequirement):
        actual = sum(
            courses[c].credits for c in all_codes
            if c in courses and courses[c].level == node.level
        )
        return {
            "type": "min_level",
            "level": node.level,
            "required": node.min_credits,
            "actual": actual,
            "passed": actual >= node.min_credits,
            "label": f"Level {node.level} credits: {actual} / {node.min_credits}cr minimum",
        }

    if isinstance(node, MaxLevelCreditsRequirement):
        actual = sum(
            courses[c].credits for c in all_codes
            if c in courses and courses[c].level == node.level
        )
        return {
            "type": "max_level",
            "level": node.level,
            "limit": node.max_credits,
            "actual": actual,
            "passed": actual <= node.max_credits,
            "label": f"Level {node.level} credits: {actual} / max {node.max_credits}cr",
        }

    if isinstance(node, ChooseCreditsRequirement):
        if node.open_pool and not node.course_codes:
            actual = sum(
                courses[c].credits for c in all_codes
                if c in courses and c not in claimed
            )
            return {
                "type": "choose_credits",
                "required": node.credits,
                "actual": actual,
                "passed": node.credits <= 0 or actual >= node.credits,
                "options": [],
                "open_pool": True,
                "label": f"Free electives: choose any {node.credits}cr: {actual}cr selected",
            }
        allowed = set(node.course_codes)
        actual = sum(courses[c].credits for c in all_codes if c in courses and c in allowed)
        return {
            "type": "choose_credits",
            "required": node.credits,
            "actual": actual,
            "passed": node.credits <= 0 or actual >= node.credits,
            "options": sorted(allowed)[:12],
            "label": f"Choose {node.credits}cr from {len(allowed)} courses: {actual}cr selected",
        }

    if isinstance(node, ChooseNRequirement):
        allowed = set(node.course_codes)
        actual_count = sum(1 for c in all_codes if c in allowed)
        return {
            "type": "choose_n",
            "required": node.n,
            "actual": actual_count,
            "passed": actual_count >= node.n,
            "options": sorted(allowed)[:12],
            "label": f"Choose {node.n} courses from {len(allowed)}: {actual_count} selected",
        }

    if isinstance(node, (AllOfRequirement, AnyOfRequirement, MajorRequirement)):
        children = [_validation_node_to_check(child, courses, all_codes, grand_total, claimed) for child in (node.children or [])]
        passed = all(c["passed"] for c in children) if isinstance(node, AllOfRequirement) else any(c["passed"] for c in children)
        label = getattr(node, "label", None) or ("All of" if isinstance(node, AllOfRequirement) else "Any of")
        return {
            "type": "group",
            "operator": "ALL" if isinstance(node, AllOfRequirement) else "ANY",
            "passed": passed,
            "label": label,
            "children": children,
        }

    return {
        "type": "unknown",
        "passed": True,
        "label": str(type(node).__name__),
        "children": [],
    }


@app.post("/api/plan/validate", summary="Validate a plan against its degree requirements")
def validate_plan(req: ValidateRequest):
    """
    Validate a plan against the full degree requirement tree.

    Accepts either:
      - a ``plan_id`` (from a previous POST /api/plan response) - loads the
        cached plan and validates it without regenerating.
      - a list of ``course_codes`` - validates those codes directly against
        the degree tree.

    When ``double_major`` is set, validates the plan against BOTH majors'
    requirement trees independently and reports both checklists - a plan
    only passes overall when it satisfies each major in full. This matters
    because a double-major plan can pass one major's requirements while
    still being short on the other's (e.g. enough credits overall, but not
    enough of the second major's specific elective pool).

    Returns a structured checklist of passed/failed requirements.
    """
    svc = _svc()
    courses = _courses()

    from coursemap.validation.engine import _collect_claimed_codes

    # Resolve which course codes to validate
    if req.plan_id:
        cached = plan_store.get(req.plan_id)
        if not cached:
            raise HTTPException(
                status_code=404,
                detail=f"Plan '{req.plan_id}' not found. Re-generate the plan first.",
            )
        # Extract all codes from every semester in the cached plan
        plan_codes: set[str] = set()
        for sem in cached.get("semesters", []):
            for c in sem.get("courses", []):
                plan_codes.add(c.get("code", ""))
        plan_codes.discard("")
        prior_codes: set[str] = set(cached.get("meta", {}).get("completed", []))
        transfer_credits = cached.get("meta", {}).get("transfer_credits", req.transfer_credits)
    else:
        plan_codes = set(req.course_codes)
        prior_codes = set(req.completed)
        transfer_credits = req.transfer_credits

    all_codes = plan_codes | prior_codes
    all_codes_ordered = list(plan_codes) + list(prior_codes)
    credits_by_code = {c: courses[c].credits for c in all_codes if c in courses}

    # Compute credit totals (shared across both majors when double_major is set -
    # a double major is one enrolment with one set of courses, not two separate totals)
    total_credits = sum(courses[c].credits for c in plan_codes if c in courses)
    prior_credits = sum(courses[c].credits for c in prior_codes if c in courses)
    grand_total = total_credits + prior_credits + transfer_credits

    majors_to_check = [req.major] + ([req.double_major] if req.double_major else [])
    results = []
    for major_name in majors_to_check:
        try:
            tree = svc.degree_tree_for_major(major_name)
        except (ValueError, AttributeError):
            tree = None
        if tree is None:
            raise HTTPException(
                status_code=404,
                detail=f"Could not build requirement tree for '{major_name}'. Check the major name.",
            )
        claimed = _collect_claimed_codes(tree, all_codes_ordered, credits_by_code)
        checklist = _validation_node_to_check(tree, courses, all_codes, grand_total, claimed)
        results.append({"major": major_name, "checklist": checklist, "passed": checklist.get("passed", False)})

    overall_passed = all(r["passed"] for r in results)

    response = {
        "major": req.major,
        "plan_id": req.plan_id,
        "overall_passed": overall_passed,
        "total_credits": grand_total,
        "plan_credits": total_credits,
        "prior_credits": prior_credits,
        "transfer_credits": transfer_credits,
        "checklist": results[0]["checklist"],
    }
    if req.double_major:
        response["double_major"] = req.double_major
        response["per_major"] = results
        response["first_major_passed"] = results[0]["passed"]
        response["second_major_passed"] = results[1]["passed"]
    return response


@app.get("/api/plan/{plan_id}/advisor-summary",
         summary="Plain-text advisor-ready summary of a stored plan")
def advisor_summary(plan_id: str):
    """
    Return a formatted plain-text summary of a stored plan suitable for
    printing or emailing to an academic advisor.
    """
    cached = plan_store.get(plan_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Plan not found.")
    text = _PlanExportSvc.to_advisor_text(cached, plan_id=plan_id)
    return Response(
        content=text, media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="degree_plan_{plan_id}.txt"'},
    )

@app.get("/api/plan/{plan_id}/markdown",
         summary="Markdown export of a stored plan")
def plan_markdown(plan_id: str):
    """Return a Markdown-formatted plan - useful for pasting into Notion, Obsidian, etc."""
    cached = plan_store.get(plan_id)
    if not cached:
        raise HTTPException(status_code=404, detail="Plan not found.")
    text = _PlanExportSvc.to_markdown(cached)
    return Response(
        content=text, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="degree_plan_{plan_id}.md"'},
    )
