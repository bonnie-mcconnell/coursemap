"""
Quick smoke-test for the prerequisite scraper.
Fetches 5 courses and checks Massey is reachable.

Run from the repo root:
    python scripts/test_prereq_scraper.py

Expected output on success:
    Testing connection to massey.ac.nz...
    OK: 159101 - Applied Programming  (prerequisites: None or a code/dict)
    OK: 159201 - Algorithms and Data...
    ...
    All 5 courses scraped successfully. Massey is reachable.
    Run the full refresh: python -m coursemap.ingestion.refresh_prerequisites
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from bs4 import BeautifulSoup
from coursemap.ingestion.prerequisite_scraper import find_prereq_text, parse_prerequisite_text

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

TEST_COURSES = [
    ("159101", "https://www.massey.ac.nz/study/courses/applied-programming-159101/"),
    ("159201", "https://www.massey.ac.nz/study/courses/algorithms-and-data-structures-159201/"),
    ("297101", "https://www.massey.ac.nz/study/courses/statistical-data-science-297101/"),
    ("160101", "https://www.massey.ac.nz/study/courses/calculus-160101/"),
    ("115113", "https://www.massey.ac.nz/study/courses/economics-for-business-115113/"),
]

print("Testing connection to massey.ac.nz...")
print()

success = 0
for code, url in TEST_COURSES:
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            text = find_prereq_text(soup)
            parsed = parse_prerequisite_text(text) if text else None
            title = soup.find("h1")
            title_text = title.get_text(strip=True)[:45] if title else "?"
            print(f"  OK: {code} - {title_text}")
            print(f"      prereqs: {parsed}")
            success += 1
        elif r.status_code == 403:
            print(f"  BLOCKED (403): {code} - Massey is blocking automated requests from this IP.")
            print("  Try using a VPN or running from a different network.")
            break
        else:
            print(f"  HTTP {r.status_code}: {code} - {url}")
    except Exception as e:
        print(f"  ERROR: {code} - {e}")
    time.sleep(0.5)

print()
if success == len(TEST_COURSES):
    print(f"All {success} courses scraped successfully. Massey is reachable.")
    print()
    print("Run the full prerequisite refresh now:")
    print("  python -m coursemap.ingestion.refresh_prerequisites --concurrency 8")
    print()
    print("This will take 15-30 minutes and update datasets/courses.json")
    print("with correct AND/OR prerequisite logic for all 2,766 courses.")
elif success > 0:
    print(f"{success}/{len(TEST_COURSES)} succeeded. Partial connectivity - check your internet connection.")
else:
    print("0 courses scraped. Massey is not reachable from this machine.")
    print("Possible causes:")
    print("  - No internet connection")
    print("  - VPN blocking massey.ac.nz")
    print("  - Massey CDN is temporarily rate-limiting this IP")
    print()
    print("The prerequisite step is optional - the planner works without it.")
    print("Prerequisites will show as AND-only (conservative) instead of AND/OR.")
