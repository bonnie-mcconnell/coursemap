#!/usr/bin/env python3
"""
Run the full data refresh in the correct order:
1. Prerequisite scraper (most important - 20-30 min)
2. Minor scraper (15 min)

Run from repo root with your venv active:
    python scripts/run_all_data_refresh.py

Or run them separately (recommended so you can check each step):
    python -m coursemap.ingestion.refresh_prerequisites --concurrency 8
    python scripts/scrape_minors.py
"""
import subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def run(cmd, desc):
    print(f"\n{'='*55}")
    print(f"STEP: {desc}")
    print(f"{'='*55}")
    start = time.time()
    result = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.time() - start
    if result.returncode == 0:
        print(f"\n✅ Done in {elapsed/60:.1f} minutes")
    else:
        print(f"\n❌ Failed after {elapsed/60:.1f} minutes (exit code {result.returncode})")
        print("You can continue manually with the next step.")
    return result.returncode == 0

print("CourseMap - Full Data Refresh")
print("This will take 35-45 minutes total. Keep this window open.")
print()
print("Step 1/2: Prerequisite scraper (~30 min)")
print("  Fetches real AND/OR prereq logic for all 2,766 courses")
run([sys.executable, '-m', 'coursemap.ingestion.refresh_prerequisites', 
     '--concurrency', '8'], 
    "Prerequisite refresh")

print("\nStep 2/2: Minor scraper (~5 min)")
print("  Scrapes all ~50 Massey minor requirement pages (39 inferred entries already present)")
run([sys.executable, 'scripts/scrape_minors.py'],
    "Minor scraper")

print(f"\n{'='*55}")
print("All done!")
print()
print("Option A (hot-reload, no restart):")
print("  curl -X POST http://localhost:8000/api/reload")
print()
print("Option B (full restart):")
print("  python -m uvicorn coursemap.api.server:app --reload --port 8000")
print(f"{'='*55}")
