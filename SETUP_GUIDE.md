# CourseMap - Complete Setup & Improvement Guide

Everything you need to do, in order, to go from v6 to a fully production-ready tool.

---

## Part 1 - Get it running (5 minutes)

### Prerequisites
- Python 3.11 or 3.12
- Windows, Mac, or Linux
- Internet access (for the optional scraping steps)

### Install and start

**Windows (easiest):** Double-click `START.bat` inside the folder. Done.

**Or manually in a terminal:**
```powershell
# 1. Unzip and enter the folder
cd coursemap_v6

# 2. Install dependencies (ignore the uvloop warning on Windows - it's fine)
pip install -r requirements.txt

# 3. Start the server - KEEP THIS TERMINAL OPEN
python -m uvicorn coursemap.api.server:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

> ⚠️ **Keep the terminal open.** The browser talks to the Python server running in that window.  
> If you close the terminal or press Ctrl+C, the page will stop working until you restart it.

**The `uvloop` error during pip install is harmless** - uvloop only works on Mac/Linux.  
Uvicorn automatically uses the standard asyncio event loop on Windows instead.

**Using the planner:**
- Click **Search majors…** in the left sidebar, or use the **Browse all 380 majors →** button
- Type any subject name (e.g. "computer science", "psychology", "finance")
- Or click one of the quick-start buttons in the middle of the screen
- Once a major is selected, click **Generate plan**

**Verify it's working:**
```powershell
# In a second terminal:
curl http://localhost:8000/api/health
```
Expected: `{"status":"ok","courses_loaded":2766,"majors_loaded":380,...}`

---

## Part 2 - Fix prerequisite data (most important, ~30 minutes)

This is the single biggest gap. Without this, ~64% of L200+ courses show a ⚠ "prerequisites unknown" warning. With it, the planner knows the real AND/OR prerequisite logic for every course.

### Step 2a - Test connectivity first (1 minute)

```powershell
python scripts/test_prereq_scraper.py
```

**Expected output:**
```
Testing connection to massey.ac.nz...

  OK: 159101 - Applied Programming
      prereqs: None
  OK: 159201 - Algorithms and Data Structures
      prereqs: {'op': 'OR', 'args': ['159101', '159102']}
  ...
  All 5 courses scraped successfully. Massey is reachable.
```

**If you see `BLOCKED (403)`:** Massey's CDN is blocking your IP. Try:
- Turn off any VPN
- Use a different network (e.g. your phone's hotspot)
- Wait an hour and try again (temporary rate limit)

**If you see connection errors:** Check your internet is working and massey.ac.nz is accessible in your browser.

### Step 2b - Run the full prerequisite refresh (20-40 minutes)

```powershell
python -m coursemap.ingestion.refresh_prerequisites --concurrency 8
```

Watch the progress. It will print something like:
```
2026-05-24 10:15:01 INFO: Processing 2766 courses with 8 workers...
2026-05-24 10:15:10 INFO: Progress: 100/2766 (4%) - 187 updated so far
2026-05-24 10:20:14 INFO: Progress: 500/2766 (18%) - 923 updated
...
2026-05-24 10:45:22 INFO: Done. 2766 scraped, 1843 prerequisites updated.
2026-05-24 10:45:22 INFO: Wrote updated courses.json.
```

**If it gets rate-limited mid-way** (you see many `HTTP 429` or `HTTP 403` errors):
```powershell
# Stop with Ctrl+C, wait 10 minutes, then resume with only-missing flag:
python -m coursemap.ingestion.refresh_prerequisites --concurrency 4 --only-missing
```

**After it finishes**, restart the server (Ctrl+C then restart) - the dataset is cached at startup.

### Step 2c - Verify it worked

```powershell
curl http://localhost:8000/api/validate
```

Look for `"prereq_coverage"` - it should now show >80% instead of ~36%.

Or open a CS plan in the browser - the ⚠ dots on L200/L300 cards should mostly disappear.

---

## Part 3 - Add all minors (~15 minutes)

Currently only 8 of Massey's ~41 minors are in the dataset. This step scrapes all of them.

### Step 3a - Run the minor scraper

```powershell
python scripts/scrape_minors.py
```

Expected output:
```
Loading courses dataset...
Existing minors: 8
Scraping 41 minors from Massey...

  Scraping: Accounting...
    ✓ 12 valid course codes
  Scraping: Agriculture...
    ✓ 9 valid course codes
  ...
  Scraping: Statistics...
    ✓ 14 valid course codes

Scraped: 39, Failed: 2
Wrote 41 minors to datasets/minors.json
```

**If some minors show 0-2 codes found:** Massey may have changed the URL for that minor.
Check the actual URL at https://www.massey.ac.nz/study/minors/ and update the `MINOR_URLS` 
dict in `scripts/scrape_minors.py` accordingly.

### Step 3b - Verify the scraped minors (important)

The scraper extracts all 6-digit codes from each minor page. This is usually accurate, 
but occasionally picks up codes from navigation or related sections.

Open `datasets/minors.json` in a text editor and spot-check 2-3 minors you know:

```json
{
  "name": "Computer Science",
  "total_credits": 120,
  "requirement": {
    "type": "ALL_OF",
    "children": [
      {
        "type": "CHOOSE_CREDITS",
        "credits": 30,
        "course_codes": ["159101", "159102"],
        "label": "Level 100 courses (30cr)"
      },
      ...
    ]
  }
}
```

Cross-check against the official minor page at massey.ac.nz/study/minors/computer-science-minor/

### Step 3c - Restart the server

```powershell
# Ctrl+C to stop, then:
python -m uvicorn coursemap.api.server:app --reload --port 8000
```

The minor selector in the sidebar should now show all 41 minors.

---

## Part 4 - Refresh offering data (optional, ~2-3 hours)

803 courses have inferred (not scraped) offering data - shown with a `?` badge in the UI. 
To get real 2026 offering data for all of them, re-run the full dataset scraper.

> ⚠️ This takes 2-3 hours and replaces all three dataset files. 
> Back up your datasets/ folder first.

```powershell
# Back up first
cp -r datasets datasets_backup

# Full re-scrape
python -m coursemap.ingestion.build_dataset
```

After this completes, run the prerequisite refresh again (Part 2), since build_dataset 
resets prerequisites to raw scraped values:

```powershell
python -m coursemap.ingestion.refresh_prerequisites --concurrency 8
```

---

## Part 5 - Keep data fresh (ongoing)

Massey updates course offerings annually (usually around October-November for the following year).

Set up a refresh schedule:

**Monthly check** (takes 2 minutes):
```powershell
curl http://localhost:8000/api/health
# Check "dataset.age_days" - if >180, it's time to re-scrape
```

**Annual re-scrape** (each November for the following year):
```powershell
python -m coursemap.ingestion.build_dataset      # 2-3 hours
python -m coursemap.ingestion.refresh_prerequisites  # 30 mins
python scripts/scrape_minors.py                  # 15 mins
```

---

## Part 6 - Deploy for other students (optional)

To run CourseMap so other students can use it, you have a few options:

### Option A - Local network (simplest)
Run with `--host 0.0.0.0` so anyone on your network can access it:
```powershell
python -m uvicorn coursemap.api.server:app --host 0.0.0.0 --port 8000
```
Share your local IP (e.g. `http://192.168.1.100:8000`).

### Option B - Free cloud hosting (recommended)

**Railway.app** (easiest, free tier available):
1. Push the repo to GitHub
2. Go to railway.app → New Project → Deploy from GitHub
3. Set start command: `uvicorn coursemap.api.server:app --host 0.0.0.0 --port $PORT`
4. Done - Railway gives you a public URL like `coursemap.railway.app`

**Render.com** (also free):
1. Push to GitHub
2. render.com → New Web Service → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn coursemap.api.server:app --host 0.0.0.0 --port $PORT`

**Important for deployment:**
- The dataset files (`datasets/*.json`) must be included in the repo - they're not auto-regenerated
- Add `--workers 2` to the uvicorn command for better concurrency
- The SQLite plan cache (`plan_store.db`) will reset on each deploy - that's fine

### Option C - Vercel / Netlify (not recommended)
These are for static sites and serverless functions. FastAPI works but the cold-start 
time (3-5s) makes it feel broken. Use Railway or Render instead.

---

## Part 7 - Extend to other universities (future)

The architecture is designed to support multiple universities. Here's what it takes:

### What you'd need for each new university:
1. **A dataset scraper** - similar to `coursemap/ingestion/fetch_courses.py` but targeting their API/website
2. **A majors scraper** - similar to `coursemap/ingestion/build_majors_dataset.py`
3. **A prerequisite scraper** - the existing `prerequisite_scraper.py` may work if their page structure is similar
4. **Degree rules** - credit totals, level distribution rules per qualification type

### Universities with accessible public data (NZ):
- **Victoria University of Wellington (Te Herenga Waka)** - course handbook at victoria.ac.nz/courses
- **University of Auckland** - catalogue.auckland.ac.nz
- **AUT** - courses.aut.ac.nz

### Quick way to add a second university:
1. Add a `university` field to each course and major in the dataset
2. Add a university selector to the sidebar
3. Filter all API calls by university
4. Run a second scraper for the new uni and append to the datasets

The hardest part is parsing each university's unique page structure. Massey's 
Swiftype search API (`swiftype_client.py`) was the key - other universities may 
use different search backends.

---

## Summary - What to do right now

| Priority | Step | Time | Impact |
|---|---|---|---|
| 🔴 Critical | Part 2: Run prereq scraper | 30 mins | Fixes 64% prereq gap - biggest improvement possible |
| 🟡 High | Part 3: Scrape all minors | 15 mins | 8 → 41 minors |
| 🟢 Medium | Part 6: Deploy to Railway | 30 mins | Other students can use it |
| ⚪ Low | Part 4: Full data refresh | 3 hours | Removes 803 `?` offering badges |
| ⚪ Future | Part 7: Add another uni | weeks | Major scope expansion |
