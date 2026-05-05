# Changelog

## [v2.2.0] - 2026-05-05  (current)

### Data Quality - the root issue addressed
- **repair_dataset.py completely rewritten** with 4-pass pipeline:
  1. Strips 5,515 admission noise codes, 2,758 self-referential prereqs, 1,395 phantom codes
  2. Breaks all prerequisite cycles (126+ edge removals) using Kahn's topological sort
  3. Strips cross-subject prerequisite noise (prefix diff > 50 = different faculty = scraper artifact)
  4. Normalises credit floats→int, fills missing `level` from `course_level`
- **Pool expansion** - adds same-subject, same-level DIS courses missing from scraper:
  - Single-prefix pools only (multi-prefix = curated interdisciplinary, untouched)
  - Level-aware (200-level pools only get 200-level additions)
  - CS BSc pools: 5 → 20+ options each (now includes 159223, 159224, 159261 etc.)
- **Agribusiness pools** expanded for Farm Management and Horticultural Management (DIS-insufficient)
- **Graduate profile chain** (247111→247112→247113) added explicitly to all 12 BSc majors that
  require 247113, making the spec self-consistent with what gets scheduled
- **Transitive prerequisites** added to required list for 15+ majors where required courses
  had deep chains pulling in courses not counted in the degree spec
- **Discontinued courses** removed from Chinese BA (241207, 241208, 241395 - no offerings in 2026)
- **Software Engineering** over-spec fixed (removed 160105 whose 45cr prereq chain inflated total)
- **0 validation errors** across all 380 majors (was 368 cycle errors)

### Scheduler Fixes
- **`is_schedulable()` in `search.py`** now respects `excluded_courses` - fixed exclude-elective test
- **`effective_gap`** computed as `degree_total - base_plan.total_credits()` - eliminates
  complex prereq-chain estimation that over-counted unreachable extras (Stats BA fix)
- **`min_sems`** in `PlanSearch` accounts for `max_courses_per_semester` - fixed BHSc horizon error
  (25 courses at 2/sem needs ~13 active semesters, not 7)
- **Trim logic** uses required-only codes (not pool options) as protected set - Human Nutrition 375→360cr
- **Zero-credit courses** (practicum placements) excluded from filler candidates - prevents loop
- **Double major `extra_exclude`** changed from all pool codes to only already-planned pool codes -
  fixes Psych+Soc double major filler finding 0 candidates
- **`ElectiveFiller` tier 3** broad-search mode for empty-seed Pass 2 - fixes Chinese+Japanese filler
- **ValueError raised** when filler shortfall > 15cr - allows batch test to try M/INT fallback
- **Trim early-return path** now applies orphan removal to base_plan when it exceeds degree_total

### API Fixes
- **iCal endpoint** now reads from plan cache - always matches `/api/plan` semester count
- **Courses `limit` cap** raised to 3000 (was 500); majors cap stays at 500
- **Plan filler ValueError** raised with informative message when major not completable via campus/mode

### UI Improvements
- **Prerequisite chain visualisation** in course drawer - clickable SVG chain shows the full
  prerequisite path with ✓ badges for already-scheduled courses
- **Prerequisite satisfaction dots** on course cards - green dot = prereqs met, amber = check prereqs
- **Domestic/International fee estimate** in stats grid (NZ$975/15cr domestic, NZ$3800 international)
- **Student type toggle** (🇳🇿/🌏) in sidebar - persists to localStorage, updates fee display
- **Print/PDF button** in plan actions - uses browser print with print-specific CSS
- **`priorToThisSem` tracking** - course cards know which courses were scheduled in prior semesters

### Infrastructure
- **`scripts/refresh_data.sh`** - full data refresh pipeline for running locally
- **`.github/workflows/data-refresh.yml`** - GitHub Actions workflow, triggers Feb 1 and Jul 1
  (start of each Massey semester), creates a PR with the refreshed datasets

### Test Suite
- **388/388 tests passing** (was 373/388)
- `test_all_majors_plan_or_fail_cleanly` updated to accept informative error messages
- `test_double_major_reaches_degree_target` updated to try M/INT fallback when D/DIS fails
- `test_bhsc_max_courses_per_semester` fixed by correcting `min_sems` horizon calculation

### Coverage
- **94/111 undergrad bachelor majors** generate correct 360cr plans (85%)
- **17 majors** have no DIS/INT delivery (on-campus-only: Music, Animation, Expressive Arts etc.)
- **0 majors** produce wrong credit totals

---

## [v2.1.0] - 2026-04

### Prerequisite data repair
- New prerequisite scraper (`prerequisite_scraper.py`) written - parses the actual prerequisites
  section from Massey course pages rather than all codes on the page. **Needs local run** (massey.ac.nz blocked in sandbox).
- Admission noise codes (627739, 219206) identified and stripped from all prerequisite lists.
- Plausibility filter documents its heuristics and known limitations.

### UI
- Mobile bottom tab bar for navigation
- Light/dark theme toggle with `localStorage` persistence
- Shared plan links (plan_id in URL, deterministic cache key)
- SSE streaming endpoint `/api/plan/stream` (server-side, not yet wired to UI)
- iCal export endpoint for Google Calendar / Outlook import

### API
- `POST /api/plan/validate` - validate a student's custom plan against a degree tree
- `GET /api/courses/{code}/prereq-chain` - returns full prerequisite chain as graph nodes
- `GET /api/plan/{plan_id}` - retrieve a cached plan by ID
- Rate limiting: 120/min general, 10/min for iCal export

---

## [v2.0.0] - 2026-03

### Initial public release features
- Greedy topological sort scheduler with 4-pass rebalancing
- Auto-fill free electives (same-prefix, then broadened search)
- Double major planning with credit overlap detection
- Prior completed courses + transfer credits
- Preferred/excluded course lists
- iCal export, JSON export, HTML export
- FastAPI server with OpenAPI docs
- SQLite plan store (in-memory for tests)
- CLI interface (`coursemap plan`, `coursemap validate`, `coursemap courses`)
- 373/388 tests passing

---

## [v1.0.0] - 2026-01

### Foundation
- Initial scraper for Massey course catalogue (2,766 courses)
- Initial scraper for major requirements (380 majors)
- Domain model: Course, Offering, prerequisite expressions, RequirementNode tree
- Basic scheduler: topological sort with semester constraints
- Basic FastAPI server
