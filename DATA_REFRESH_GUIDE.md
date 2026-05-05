# Data Refresh Guide

## Why this is needed

The Massey University course catalogue changes each semester:
- New courses added / old courses discontinued
- Prerequisites updated
- Delivery modes changed (DIS/INT/BLK)
- Major requirement structures revised

The deployment sandbox **cannot reach massey.ac.nz** (network policy). Data refresh must be run **locally** by a developer with internet access.

## Cadence

| Semester | Start | Refresh by |
|---------|-------|-----------|
| S1 | Feb | Jan 25 |
| S2 | Jul | Jun 25 |

The GitHub Actions workflow (`data-refresh.yml`) creates a PR automatically on Feb 1 and Jul 1, but **someone must run it locally** since the CI runner also can't reach Massey.

## How to refresh

```bash
# Clone and install
git clone <repo>
cd coursemap_project
pip install -e ".[dev]"

# Full refresh (30–45 min, needs internet)
./scripts/refresh_data.sh

# Or dry-run (just repair + validate, no scrape)
./scripts/refresh_data.sh --dry-run

# Only re-scrape courses (faster, if only offerings changed)
./scripts/refresh_data.sh --courses-only
```

## What the refresh does

1. **Backs up** `datasets/courses.json` and `datasets/majors.json`
2. **Scrapes** the Massey course catalogue (`build_courses_dataset.py`)
3. **Scrapes** major requirements (`build_majors_dataset.py`)
4. **Runs `repair_dataset.py`** which:
   - Strips self-referential and noise prerequisites
   - Breaks prerequisite cycles (Kahn's algorithm)
   - Strips cross-subject prerequisite noise (prefix diff > 50)
   - Expands single-prefix elective pools with missing same-level courses
5. **Validates** the dataset (`coursemap validate`)
6. **Runs tests** (`pytest tests/ -q`)

## After refresh: manual audit checklist

Before merging the PR:

- [ ] `python -m coursemap.validation.dataset_validator` → 0 errors
- [ ] `pytest tests/ -q` → 388/388 pass
- [ ] CS BSc pools: at least 15 options each? `python -c "import json; d=json.load(open('datasets/majors.json')); cs=next(m for m in d if 'Computer Science' in m['name'] and 'Bachelor of Science' in m['name']); [print(ch['credits'], len(ch['course_codes'])) for ch in cs['requirement']['children'] if ch.get('type')=='CHOOSE_CREDITS']"`
- [ ] BHSc plan generates 360cr? `python -c "from coursemap.ingestion.dataset_loader import *; from coursemap.services.planner_service import *; c=load_courses(); m=load_majors(); s=PlannerService(c,m); p=s.generate_best_plan('Mental Health and Addiction – Bachelor of Health Science', campus='D', mode='DIS', start_year=2026); print(p.total_credits())"`
- [ ] Human Nutrition generates 360cr (a known historically-tricky major)?

## Known limitations of the scraped data

### Prerequisite completeness
The `build_courses_dataset.py` scraper extracts prerequisites from the entire course page, not just the prerequisites section. This means:
- AND/OR logic is inferred, not captured
- Cross-subject prerequisites include noise (e.g. Health courses accidentally listing Animal Science codes)
- `repair_dataset.py` strips the most egregious noise using heuristics

**For truly correct prerequisites**, the `prerequisite_scraper.py` module uses targeted HTML parsing of the prerequisites section. Run it after a scrape:

```bash
python -m coursemap.ingestion.prerequisite_scraper \
  --input datasets/courses.json \
  --output datasets/courses.json \
  --sleep 1.0
```

This takes longer but produces significantly better prerequisite data.

### Major requirement completeness
The `build_majors_dataset.py` scraper uses Massey's qualification web pages. These sometimes:
- List only a subset of elective pool options
- Miss courses offered via multiple delivery modes
- Have structural inconsistencies between Bachelor majors and their qualification wrappers

The `repair_dataset.py` pool expansion addresses the most common shortfalls.

## Deploying updated data

After merging the PR:

```bash
# If running locally
cp datasets/courses.json /path/to/deployment/datasets/
cp datasets/majors.json  /path/to/deployment/datasets/

# If using Docker, rebuild the image:
docker build -t coursemap .
docker push coursemap:latest

# The server hot-reloads datasets on startup - restart the container.
```
