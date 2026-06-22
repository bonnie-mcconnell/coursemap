#!/usr/bin/env bash
# scripts/refresh_data.sh - Full data refresh pipeline for coursemap
# Run LOCALLY (needs massey.ac.nz access). Cadence: start of each semester.
# Usage: ./scripts/refresh_data.sh [--dry-run] [--courses-only] [--majors-only]

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

DRY_RUN=0; COURSES_ONLY=0; MAJORS_ONLY=0
for arg in "$@"; do
  case "$arg" in --dry-run) DRY_RUN=1;; --courses-only) COURSES_ONLY=1;; --majors-only) MAJORS_ONLY=1;; esac
done

echo "=== coursemap data refresh $(date +%Y-%m-%d) ==="
BACKUP="datasets/backups/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP"
cp datasets/courses.json "$BACKUP/" 2>/dev/null || true
cp datasets/majors.json  "$BACKUP/" 2>/dev/null || true
echo "✓ Backed up datasets to $BACKUP"

if [ "$DRY_RUN" = "1" ]; then
  echo "(dry-run: repair + validate only)"
  python3 -m coursemap.ingestion.repair_dataset
  python3 -m coursemap.validation.dataset_validator --report
  exit 0
fi

[ "$MAJORS_ONLY" = "0" ] && {
  echo "── Scraping courses (10–30 min)…"
  python3 -m coursemap.ingestion.build_courses_dataset --output datasets/courses.json --sleep 0.5
  echo "✓ Courses done"
}

[ "$COURSES_ONLY" = "0" ] && {
  echo "── Scraping majors (5–15 min)…"
  python3 -m coursemap.ingestion.build_majors_dataset --output datasets/majors.json --sleep 0.5
  echo "✓ Majors done"
}

echo "── Repairing data…"
python3 -m coursemap.ingestion.repair_dataset
echo "── Patching free-elective gaps…"
python3 -m coursemap.ingestion.patch_elective_gaps
# Clear plan cache so stale plans aren't served after data update
[ -f "data/plans.db" ] && rm "data/plans.db" && echo "── Cleared plan cache"
echo "── Validating…"
python3 -m coursemap.validation.dataset_validator --report
echo "── Testing…"
python3 -m pytest tests/ -q --tb=short

echo "=== Done. Review: git diff --stat datasets/ ==="
