#!/usr/bin/env python3
"""
Scrape Massey minor requirements from their website and add them to minors.json.

Run from the repo root:
    python scripts/scrape_minors.py

This fetches each minor's page from massey.ac.nz, parses the required courses,
and updates datasets/minors.json.

Requires internet access to massey.ac.nz.
"""
from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
MINORS_PATH = ROOT / "datasets" / "minors.json"
COURSES_PATH = ROOT / "datasets" / "courses.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

# All known Massey minors with their URL slugs
MINOR_URLS = {
    "Accounting":                   "https://www.massey.ac.nz/study/minors/accounting-minor/",
    "Agriculture":                  "https://www.massey.ac.nz/study/minors/agriculture-minor/",
    "Animal Science":               "https://www.massey.ac.nz/study/minors/animal-science-minor/",
    "Biochemistry":                 "https://www.massey.ac.nz/study/minors/biochemistry-minor/",
    "Biology":                      "https://www.massey.ac.nz/study/minors/biology-minor/",
    "Business":                     "https://www.massey.ac.nz/study/minors/business-minor/",
    "Chemistry":                    "https://www.massey.ac.nz/study/minors/chemistry-minor/",
    "Communication":                "https://www.massey.ac.nz/study/minors/communication-minor/",
    "Computer Science":             "https://www.massey.ac.nz/study/minors/computer-science-minor/",
    "Creative Writing":             "https://www.massey.ac.nz/study/minors/creative-writing-minor/",
    "Criminology":                  "https://www.massey.ac.nz/study/minors/criminology-minor/",
    "Defence and Security Studies": "https://www.massey.ac.nz/study/minors/defence-and-security-studies-minor/",
    "Ecology":                      "https://www.massey.ac.nz/study/minors/ecology-minor/",
    "Economics":                    "https://www.massey.ac.nz/study/minors/economics-minor/",
    "Education":                    "https://www.massey.ac.nz/study/minors/education-minor/",
    "English":                      "https://www.massey.ac.nz/study/minors/english-minor/",
    "Environmental Studies":        "https://www.massey.ac.nz/study/minors/environmental-studies-minor/",
    "Finance":                      "https://www.massey.ac.nz/study/minors/finance-minor/",
    "Food Technology":              "https://www.massey.ac.nz/study/minors/food-technology-minor/",
    "Geography":                    "https://www.massey.ac.nz/study/minors/geography-minor/",
    "Health Science":               "https://www.massey.ac.nz/study/minors/health-science-minor/",
    "History":                      "https://www.massey.ac.nz/study/minors/history-minor/",
    "Human Resource Management":    "https://www.massey.ac.nz/study/minors/human-resource-management-minor/",
    "Information Systems":          "https://www.massey.ac.nz/study/minors/information-systems-minor/",
    "International Relations":      "https://www.massey.ac.nz/study/minors/international-relations-minor/",
    "Law and Society":              "https://www.massey.ac.nz/study/minors/law-and-society-minor/",
    "Maori Studies":                "https://www.massey.ac.nz/study/minors/maori-studies-minor/",
    "Marketing":                    "https://www.massey.ac.nz/study/minors/marketing-minor/",
    "Mathematics":                  "https://www.massey.ac.nz/study/minors/mathematics-minor/",
    "Media Studies":                "https://www.massey.ac.nz/study/minors/media-studies-minor/",
    "Music":                        "https://www.massey.ac.nz/study/minors/music-minor/",
    "Philosophy":                   "https://www.massey.ac.nz/study/minors/philosophy-minor/",
    "Physics":                      "https://www.massey.ac.nz/study/minors/physics-minor/",
    "Politics":                     "https://www.massey.ac.nz/study/minors/politics-minor/",
    "Property":                     "https://www.massey.ac.nz/study/minors/property-minor/",
    "Psychology":                   "https://www.massey.ac.nz/study/minors/psychology-minor/",
    "Public Health":                "https://www.massey.ac.nz/study/minors/public-health-minor/",
    "Social Policy":                "https://www.massey.ac.nz/study/minors/social-policy-minor/",
    "Sociology":                    "https://www.massey.ac.nz/study/minors/sociology-minor/",
    "Sport and Exercise":           "https://www.massey.ac.nz/study/minors/sport-and-exercise-minor/",
    "Statistics":                   "https://www.massey.ac.nz/study/minors/statistics-minor/",
}

CODE_RE = re.compile(r"\b(\d{6})\b")


def extract_codes_from_page(soup: BeautifulSoup) -> list[str]:
    """Extract all 6-digit course codes mentioned on the page."""
    # Try structured tables first
    codes = []
    
    # Look for course code patterns in tables, lists, and paragraphs
    for tag in soup.find_all(["td", "li", "p", "dd"]):
        text = tag.get_text()
        found = CODE_RE.findall(text)
        codes.extend(found)
    
    # Deduplicate preserving order
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def build_requirement_tree(codes: list[str], courses_map: dict) -> dict:
    """
    Build a simple requirement tree from a list of course codes.
    Groups into L100, L200, L300 buckets.
    """
    l100 = [c for c in codes if courses_map.get(c, {}).get("level", 0) == 100]
    l200 = [c for c in codes if courses_map.get(c, {}).get("level", 0) == 200]
    l300 = [c for c in codes if courses_map.get(c, {}).get("level", 0) == 300]
    other = [c for c in codes if c not in l100 and c not in l200 and c not in l300]
    
    children = []
    if l100:
        l100_cr = min(len(l100) * 15, 45)  # typical L100 minor component
        children.append({
            "type": "CHOOSE_CREDITS",
            "credits": l100_cr,
            "course_codes": l100,
            "label": f"Level 100 courses ({l100_cr}cr)"
        })
    if l200:
        l200_cr = min(len(l200) * 15, 60)
        children.append({
            "type": "CHOOSE_CREDITS", 
            "credits": l200_cr,
            "course_codes": l200,
            "label": f"Level 200 courses ({l200_cr}cr)"
        })
    if l300:
        l300_cr = min(len(l300) * 15, 45)
        children.append({
            "type": "CHOOSE_CREDITS",
            "credits": l300_cr,
            "course_codes": l300,
            "label": f"Level 300 courses ({l300_cr}cr)"
        })
    if other:
        children.append({
            "type": "CHOOSE_CREDITS",
            "credits": len(other) * 15,
            "course_codes": other,
            "label": f"Other courses ({len(other) * 15}cr)"
        })
    
    return {
        "type": "ALL_OF",
        "children": children,
        "label": "Minor requirements (120cr)"
    }


def scrape_minor(name: str, url: str, courses_map: dict) -> dict | None:
    """Scrape a single minor page and return a minor dict."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 404:
            print(f"  404: {name} - URL may have changed: {url}")
            return None
        if r.status_code != 200:
            print(f"  HTTP {r.status_code}: {name}")
            return None
        
        soup = BeautifulSoup(r.text, "html.parser")
        codes = extract_codes_from_page(soup)
        
        # Filter to only codes in our courses dataset
        valid_codes = [c for c in codes if c in courses_map]
        
        if len(valid_codes) < 3:
            print(f"  ⚠ {name}: only {len(valid_codes)} valid codes found - check manually")
        
        req_tree = build_requirement_tree(valid_codes, courses_map)
        
        return {
            "name": name,
            "url": url,
            "total_credits": 120,
            "requirement": req_tree,
            "data_quality": "scraped",
            "scraped_codes": valid_codes,
            "note": f"Scraped from {url}. Verify with official Massey minor regulations."
        }
    except Exception as e:
        print(f"  ERROR: {name} - {e}")
        return None


def main():
    # Load existing data
    print("Loading courses dataset...")
    with open(COURSES_PATH, encoding="utf-8") as f:
        courses = json.load(f)
    courses_map = {c["course_code"]: c for c in courses}
    
    # Load existing minors (preserve hand-curated ones)
    existing_minors = {}
    if MINORS_PATH.exists():
        with open(MINORS_PATH, encoding="utf-8") as f:
            for m in json.load(f):
                existing_minors[m["name"]] = m
    
    print(f"Existing minors: {len(existing_minors)}")
    print(f"Scraping {len(MINOR_URLS)} minors from Massey...")
    print()
    
    new_minors = dict(existing_minors)  # start with existing
    scraped = 0
    failed = 0
    
    for name, url in MINOR_URLS.items():
        print(f"  Scraping: {name}...")
        minor = scrape_minor(name, url, courses_map)
        if minor:
            new_minors[name] = minor
            n_codes = len(minor.get("scraped_codes", []))
            print(f"    ✓ {n_codes} valid course codes")
            scraped += 1
        else:
            failed += 1
        time.sleep(0.8)  # polite delay
    
    print()
    print(f"Scraped: {scraped}, Failed: {failed}")
    
    # Write output
    minors_list = sorted(new_minors.values(), key=lambda m: m["name"])
    with open(MINORS_PATH, "w", encoding="utf-8") as f:
        json.dump(minors_list, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(minors_list)} minors to {MINORS_PATH}")
    print()
    print("⚠ IMPORTANT: Review the scraped minors in datasets/minors.json")
    print("  The scraper extracts ALL course codes from each page, which may include")
    print("  codes from related programmes, examples, or navigation. Verify key minors")  
    print("  manually against the official Massey handbook.")
    print()
    print("Restart the server to pick up the new minors:")
    print("  python -m uvicorn coursemap.api.server:app --reload --port 8000")


if __name__ == "__main__":
    main()
