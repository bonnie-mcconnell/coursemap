# Data Quality & Improvement Guide

This document explains what data the planner uses, where it comes from, its known gaps, and how to improve it.

---

## Dataset Overview

| File | Records | Source | Last updated |
|---|---|---|---|
| `datasets/courses.json` | 2,766 courses | Massey Swiftype API + page scraping | Scraped 2026 |
| `datasets/majors.json` | 380 majors | Massey Swiftype API + requirement pages | Scraped 2026 |
| `datasets/qualifications.json` | 176 qualifications | Massey Swiftype API | Scraped 2026 |
| `datasets/minors.json` | 39 minors | Hand-curated from the Massey handbook | Hand-curated |

All course `description` fields are auto-generated boilerplate (level/credits/campus summary), not the real Massey course description. Click the ↗ link on any course to see the actual page.

---

## Prerequisite Coverage

| Status | Count (L200-400, undergrad) | Count (L200+, all levels incl. postgrad) |
|---|---|---|
| Has prerequisite data recorded | 770 (66.3%) | 867 (36.4%) |
| No prerequisite data recorded | 391 (33.7%) | 1,512 (63.6%) |

The undergrad-only figure is probably the more useful one for most students - a third of L200-400 courses are missing prerequisite data. The all-levels figure is much higher because many postgrad papers (especially research/thesis courses) genuinely have no prerequisite beyond admission to the programme; that's not all scraping failure, but it hasn't been separated out from genuine gaps, so the UI indicator (below) flags both the same way.

"No prerequisite data recorded" means the scraper didn't capture anything for that course - it does NOT mean the course has no prerequisite. Treat it as "unverified," not "confirmed none."

**UI indicator:** course cards show a small dot next to the course code:
- 🟢 green - prerequisites recorded and satisfied by courses earlier in the plan
- 🟠 amber - prerequisites recorded but NOT yet satisfied by this point in the plan
- 🔵 blue - no prerequisite data recorded for this L200+ course (unverified, not confirmed-none)
- *(no dot)* - L100 course, or a course confirmed to genuinely have no prerequisite

The course drawer shows the same distinction in the Prerequisites section, with a direct link to the course's real Massey page when data is unverified.

**Known specific errors found and fixed during a manual audit** (cross-checked against Massey's live qualification regulations pages) - if you find more, the pattern to check is AND vs OR confusion and missing prerequisite chains, not just missing data:
- 159251 (Software Engineering Design and Construction): was recorded with no prerequisite; actually requires 159234
- 159302 (Artificial Intelligence): was recorded as 159234 AND 159201; actually 159201 OR 159234
- 159356 (Software Engineering Capstone): was missing 159352 from its prerequisite
- 159342 (Operating Systems and Networks): was AND, actually OR (159201 or 159234)
- 159352 (Advanced Web Development): had the wrong courses entirely; actually (159100 or 159101) and 158258
- 159361 (Advanced Games Programming): was missing 159235
- 159336 (Mobile Application Development): was AND, actually OR (159234 or 159235)
- 158222 (Data Wrangling and Machine Learning): had the wrong courses entirely; approximated as (159100 or 159101) and (one of 160101/160102/161111/161122) - Massey's actual regulation references a "1611xx" prefix pattern that isn't expressible in the current schema, see the note on choose-N/prefix-pattern prerequisites below
- 159333 (Programming Project): real requirement is "two of 158222, 1592xx" (a choose-2-from-pool requirement) - approximated as requiring 159201 alone, since this schema has no construct for "N of M courses" as a prerequisite. This is a known approximation, not a verified match.

### How to fix prerequisite data (requires Windows/Mac with internet access)

**Step 1 - Test connectivity:**
```bash
python scripts/test_prereq_scraper.py
```
Expected output: 5 courses scraped, all showing prerequisites.

**Step 2 - Run the full refresh (~2–3 hours for all 2,766 courses):**
```bash
python -m coursemap.ingestion.refresh_prerequisites
```
This fetches each course's Massey page, parses real AND/OR prerequisite logic, and updates `courses.json`.

**Options:**
```bash
# Only re-scrape courses with no prerequisite data (faster):
python -m coursemap.ingestion.refresh_prerequisites --only-missing

# Test with first 50 courses:
python -m coursemap.ingestion.refresh_prerequisites --limit 50

# Reduce concurrency if getting rate-limited:
python -m coursemap.ingestion.refresh_prerequisites --concurrency 5
```

**Known schema limitation:** the prerequisite expression tree only supports AND/OR over fixed course codes. It cannot express "choose N of M courses" (e.g. 159333's "two of 158222, 1592xx") or prefix-pattern alternatives (e.g. "any 1611xx-coded course"). Courses with this kind of requirement are necessarily approximated. If you're extending this dataset, search for other courses with similarly-phrased regulations before assuming a simple AND/OR captures them correctly.

---

## Offering Data Gaps

**323 courses** (marked with `?` in the UI) are required in major programmes but have no confirmed 2026 offering data. These were given inferred offerings based on:
- Patterns from other courses in the same subject prefix
- Whether the course title suggests it is a flexible "Special Topic" or thesis

To get real offering data, the scraper must be re-run. The full refresh pipeline is:
```bash
python -m coursemap.ingestion.build_dataset
```

---

## Majors with Weak Data

**4 majors** have no specific required course codes (entire degree is "free electives"):
- Biological Sciences – Postgraduate Diploma in Science and Technology
- Expressive Arts and Media Studies – Bachelor of Communication
- Without Specialisation – Graduate Certificate in Arts
- Without Specialisation – Graduate Diploma in Arts

**89 majors** have >200cr of free elective gaps (the major requirement tree only specifies a subset of the degree).

For these majors, the planner fills electives automatically but the result is not based on real programme regulations.

**~36 majors (9.5%)** have scraped data listing more "required" courses than the degree's credit target allows for - e.g. a 120cr diploma whose major data lists 165cr of compulsory-looking course codes. This is concentrated in postgraduate Psychology, Education, and Māori Studies programmes, and is almost always because the source data flattened several alternative specialisation tracks into one list, rather than the degree genuinely requiring more credits than it awards.

The planner copes with this by capping the *droppable* portion of the required-course list to fit the credit budget - but it will never drop a course the degree's own validation tree explicitly checks as required, even if that means the final plan ends up *over* the degree's credit target. This was a deliberate fix made after auditing this dataset: an earlier version of the trim logic counted only each kept course's own credits with no visibility into prerequisite-chain cost, which let it sometimes drop a genuinely-required course purely because the (incomplete) credit math looked like there was room to spare - producing a plan that *looked* complete (right total credits) but silently failed validation for whichever course got dropped. An over-the-target credit total is a more honest failure mode than a plausible-looking one that's secretly missing a required course: it's visibly wrong rather than invisibly wrong.

For a handful of majors with this pattern, the result is therefore a plan that legitimately can't fit within the stated degree length using only DIS-offered courses - that's a real fact about the source data (more required courses + prerequisite chains than the degree credits for), not something a planning algorithm can paper over. The actual fix is re-scraping these specific majors' requirement pages and modelling the alternative tracks as real choice structures, not a smarter scheduler.

---

## Minors

The 39 minors in `datasets/minors.json` are hand-curated from the Massey handbook. They are approximate:
- Course code lists may not match the current year's handbook
- Level requirements (L100/200/300 credit splits) are typical but may vary by programme
- These have NOT been individually cross-checked against live Massey pages the way the Computer Science prerequisite chain was - treat them with the same "verify before enrolling" caution as any unverified prerequisite data

To add more minors, edit `datasets/minors.json` following the existing structure.

---

## Re-scraping the Full Dataset

> ⚠️ Requires Massey's website to be accessible (blocked in some server/CI environments).

```bash
# Full re-scrape (takes several hours):
python -m coursemap.ingestion.build_dataset

# Faster: only refresh courses, not majors/quals:
python -m coursemap.ingestion.refresh_prerequisites

# After any re-scrape, repair the data:
python -m coursemap.ingestion.repair_dataset
```

After re-scraping, restart the server - the dataset is cached at startup.

