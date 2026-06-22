# CourseMap - Student Guide

**CourseMap** is an unofficial degree planner for Massey University. It generates semester-by-semester course schedules based on your chosen major, campus, and study preferences.

> ⚠️ **This tool is not affiliated with Massey University.** Always verify your plan with [official Massey programme information](https://www.massey.ac.nz/study/) and your academic advisor before enrolling.

---

## Quick Start

```bash
cd coursemap_v4
pip install -r requirements.txt
python -m uvicorn coursemap.api.server:app --reload --port 8000
```

Then open **http://localhost:8000** in your browser.

---

## How to Use

### 1. Pick your major
Type your major name in the search box. Use the filter pills (Bachelor, Honours, Postgrad…) to narrow down.

**Examples:**
- `Computer Science – Bachelor of Information Sciences`
- `Psychology – Bachelor of Arts`
- `Finance – Bachelor of Business`

### 2. Set your options
| Setting | What it does |
|---|---|
| **Start year/semester** | When you begin (auto-detected) |
| **Study load** | Full-time = 60cr/sem, Part-time = 30cr/sem |
| **Campus** | D=Distance, M=Manawatū, A=Auckland, W=Wellington |
| **Mode** | DIS=Distance/online, INT=Internal, BLK=Block |
| **Transfer credits** | If you have recognised prior learning |
| **Double major** | Add a second major to share courses |

### 3. Personalise
- **Already completed** - Add courses you've already passed (removes them from the plan)
- **Preferred electives** - Course codes you want the planner to prioritise for free electives
- **Exclude** - Course codes to never include

### 4. Add a minor (beta)
Select a minor from the dropdown. The planner will add its key courses to your preferred electives list. Minor scheduling is approximate - verify with Massey.

### 5. Generate and review
Click **Generate plan**. Review each semester. Click any course card to see:
- Full description and prerequisites
- Available offerings (campus/semester)
- Estimated fee for that course
- Quick actions: Mark done, Prefer, Exclude

---

## Understanding Your Plan

### Course card badges
| Badge | Meaning |
|---|---|
| `elective ↔` | Free elective - swap for any course at the same level |
| `×2` | Shared course (counts toward both majors in a double major) |
| `?` (amber circle) | **Offering not confirmed for 2026** - verify availability at Massey |
| 🟢 dot | Prerequisites appear to be met |
| 🔴 dot | Prerequisites may be missing - check before enrolling |

### Stats panel
- **Semesters** - Total scheduled semesters
- **Credits planned** - Total course credits in your plan
- **Degree target** - Credits required for your qualification
- **Elective gap** - Credits not yet assigned (enable Auto-fill to resolve)
- **Est. total fees** - Rough estimate using 2026 rates by subject area. **Not guaranteed.**

### Fee estimates
Fees are estimated using Massey's published 2026 rate ranges, divided by credit value and adjusted per subject area. They are approximate only. Use the [Massey fee estimator](https://www.massey.ac.nz/fees) for exact figures.

---

## Known Limitations

### Prerequisite data (~36% complete for L200+ courses)
Prerequisite information for ~64% of L200+ courses is either missing or was inferred from patterns. The planner shows a 🔴 dot on cards where prerequisites cannot be verified.

**What to do:** Before enrolling in any L200+ course, manually check its prerequisites on [Massey's course pages](https://www.massey.ac.nz/study/all-programmes-of-study/).

**To fix this permanently:** Run the prerequisite scraper (see [DATA_QUALITY.md](DATA_QUALITY.md)).

### Courses marked with `?`
These 323 courses are required in major programmes but have no confirmed 2026 offering data. The planner infers they run based on historical patterns, but you **must** verify they are offered in your intended year/semester.

### Minors (beta)
Minor support is approximate. The planner adds minor courses to your preferred electives, but cannot guarantee a correctly-sequenced minor path. Always check [massey.ac.nz/study/minors](https://www.massey.ac.nz/study/minors/) for the official minor structure.

### What the planner doesn't know about
- GPA-gated courses (some courses require a minimum GPA to enrol)
- Enrolment caps (popular courses can fill up)
- Conjoint degrees
- Elective restrictions ("choose from List A only" for some degrees)
- Courses offered in alternating years

---

## Exporting Your Plan

| Export | Use case |
|---|---|
| **↓ JSON** | Machine-readable plan data |
| **↓ iCal** | Import into Google Calendar / Outlook |
| **↓ HTML** | Save as a printable document |
| **⎙ Print** | Print or save as PDF |
| **Share link** | Copy a URL that re-generates this plan |

---

## Before You Enrol - Checklist

1. ☐ Verify programme requirements at [massey.ac.nz/study](https://www.massey.ac.nz/study/)
2. ☐ Check prerequisites for every L200+ course manually
3. ☐ Confirm course availability for your specific enrolment year
4. ☐ If adding a minor, verify minor requirements separately
5. ☐ Check actual fees at [massey.ac.nz/fees](https://www.massey.ac.nz/fees)
6. ☐ Book a session with your [academic advisor](https://www.massey.ac.nz/student-life/student-services/student-advisory-services/)
