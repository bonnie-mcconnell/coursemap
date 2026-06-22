# CourseMap v7

Unofficial Massey University degree planner. Plan a full bachelor's degree - single or double major - see prerequisite chains, browse courses, and export your plan for an advisor meeting.

**⚠ Unofficial tool. Always verify your plan with Massey University and your academic advisor before enrolling.**

---

## Quick start

```powershell
cd coursemap_v7
pip install -e ".[all]"
python -m uvicorn coursemap.api.server:app --reload --port 8000
```

Open http://localhost:8000

---

## After running the prerequisite scraper

The dataset ships with ~36% prerequisite coverage for L200+ courses. Running the scraper fills this to ~90%+ with real AND/OR logic from Massey's course pages:

```powershell
python scripts/run_all_data_refresh.py   # ~30 min, requires internet
```

Then reload the server without restarting:

```
POST /api/reload
```

Or just restart: `python -m uvicorn coursemap.api.server:app --reload --port 8000`

---

## What's in v7

### Bug fixes
- `full_year` flag added to `Offering` - "Full Year" courses (must enrol S1+S2 as a unit) are now distinguished from "Double Semester" courses (can take either). Plans warn you when full-year courses appear.
- `transfer_credits` now validated with an upper bound of 360 (previously unbounded).
- `_svc._specs_cache` monkey-patch replaced with a clean module-level `_specs_cache_store` dict.
- `no_summer` defaults to `True` in both the API schema and UI checkbox (Summer School is opt-in).
- Zero-credit courses (practicums, placements) now return a `_note` warning in the course detail API response.

### New API endpoints
| Endpoint | Description |
|---|---|
| `POST /api/reload` | Hot-reload datasets without restarting the server |
| `POST /api/plan/compare` | Compare 2–4 majors side by side (semesters, gap, coverage) |
| `GET /api/plan/{id}/advisor-summary` | Plain-text plan export for printing/emailing to advisor |
| `GET /api/plan/{id}/markdown` | Markdown export for Notion, Obsidian, etc. |
| `GET /api/courses?semester=S1` | New `semester` filter on course browser (S1, S2, SS) |
| `GET /api/courses?min_credits=1` | Exclude zero-credit practicum courses from browser |
| `POST /api/plan/progress` | Mid-degree progress check: % complete, required met/remaining, semesters left |
| `GET /api/minors?data_quality=inferred` | Filter minors by scraped vs inferred data quality |

### New services
- `DegreeInfoService` - focused read-only queries: total credits, free elective gap, required codes, prereq coverage
- `PlanExportService` - formats stored plans as plain text or Markdown (no HTTP dependency)
- `plan_store.async_get / async_put` - non-blocking wrappers using `asyncio.to_thread`

### UI improvements
- **Full Year badge** - `FY` pill on course cards where full-year enrolment is required
- **Gap banner** now includes a human-readable `gap_explanation` from the API, plus action buttons ("Auto-fill" and "Add second major")
- **⇄ Compare** button opens a side-by-side major comparison table
- **↓ Advisor** button downloads plain-text plan summary
- **↓ MD** button downloads Markdown plan export
- **Semester filter pills** in course browser modal (All / Sem 1 / Sem 2 / Summer)
- **Visual prereq graph** button in course drawer - opens a layered SVG DAG in a new window
- Prerequisites chain section now has a "Visual prereq graph ↗" button

---

## Known data gaps

| Gap | Severity | Fix |
|---|---|---|
| ~64% of L200+ courses missing prerequisite data | High | Run `python scripts/run_all_data_refresh.py` |
| Only 39 of 50+ Massey minors present - inferred entries need scraper confirmation | Low | Run `python -m coursemap.ingestion.minor_scraper` |
| Elective restrictions not modelled ("choose from List A") | Medium | Requires manual verification of programme regulations |
| Zero-credit courses (practicum/placement) visible in browser | Low | Filter excluded from auto-schedule; warning shown in detail view |

---

## What's new in v7 (continuation)

- **Minors expanded** from 8 to 39 using course-prefix inference. Scraped entries have real Massey data; inferred are pattern-matched. Always verify at massey.ac.nz/study/minors/
- **`ElectiveFiller._prereqs_satisfiable`** fixed to handle OR expressions correctly - previously required ALL branches of an OR to be present (too strict), now requires ANY one branch
- **`POST /api/plan/progress`** - mid-degree progress check: credits earned, % complete, required courses met/remaining, estimated semesters remaining, on-track warning if L300+ before 60cr
- **Double-major `gap_explanation`** now correctly says "overlap" rather than repeating the single-major BInfSc message
- **Cache version bumped** to `v7.0` - all v6 cached plans auto-invalidated
- **API version** updated to `7.0.0`
- **`/api/minors`** now exposes `total`, `scraped`, `inferred` counts and supports `?data_quality=` filter
- **Progress bar** in UI - when completed courses exist and `credits_prior > 0`, a green progress bar shows degree completion percentage
- **Minor label** updated from "beta" to "39 available" in sidebar
- Dead `_REBALANCE_THRESHOLD_POSTGRAD` constant removed from generator

## Test suite

```powershell
python -m pytest tests/ -q
```

**441 tests, 0 failures.**

---

## Architecture

```
coursemap/
  api/
    server.py           API routes (FastAPI) - 1,350 lines
    plan_store.py       SQLite plan cache with async wrappers
    ui.html             Single-file frontend (vanilla JS)
  domain/
    course.py           Course, Offering (with full_year flag)
    plan.py             DegreePlan, SemesterPlan
    prerequisite.py     PrerequisiteExpression, AndExpr, OrExpr
  ingestion/
    dataset_loader.py   Loads courses.json → domain objects
    refresh_prerequisites.py   Massey scraper (run manually)
  planner/
    generator.py        Greedy topological scheduler
    elective_filler.py  Free elective pool selection
  rules/
    degree_rules.py     Derives requirement tree from qual + major data
  services/
    planner_service.py  Main orchestration (1,582 lines - refactor target)
    degree_info_service.py     Read-only degree metadata queries
    plan_export_service.py     Plain-text and Markdown export formatting
  validation/
    engine.py           Requirement tree satisfaction checker
  optimisation/
    search.py           Branch-and-bound plan optimiser
datasets/
  courses.json          2,766 courses with offerings, credits, prereqs
  majors.json           380 majors with requirement trees
  minors.json           8 minors (beta)
  qualifications.json   176 qualifications
```
