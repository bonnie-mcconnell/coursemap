# scripts/refresh_data.ps1 - Full data refresh pipeline for coursemap (Windows)
# Run LOCALLY from your machine - needs internet access to massey.ac.nz
# Cadence: start of each semester (February and July).
#
# Requirements:
#   - Python 3.10+ in PATH
#   - pip install -r requirements.txt  (run once after unzipping)
#
# Usage:
#   .\scripts\refresh_data.ps1              # full refresh (30-60 min)
#   .\scripts\refresh_data.ps1 -DryRun      # validate only, no changes
#   .\scripts\refresh_data.ps1 -PrereqOnly  # only re-scrape prerequisites (~20 min)
#   .\scripts\refresh_data.ps1 -SkipPrereq  # skip prereq scraping (faster)

param(
    [switch]$DryRun,
    [switch]$PrereqOnly,
    [switch]$SkipPrereq
)

$ErrorActionPreference = "Stop"
$Repo = $PSScriptRoot | Split-Path -Parent
Set-Location $Repo

$Date = Get-Date -Format "yyyy-MM-dd HH:mm"
Write-Host ""
Write-Host "=== coursemap data refresh - $Date ===" -ForegroundColor Cyan
Write-Host ""

# Check Python is available
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python not found. Install from https://python.org/downloads/ and re-run."
    exit 1
}

$PythonVersion = python --version 2>&1
Write-Host "Using $PythonVersion" -ForegroundColor Green

# Backup existing datasets
$BackupDir = "datasets\backups\$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
if (Test-Path "datasets\courses.json") { Copy-Item "datasets\courses.json" $BackupDir }
if (Test-Path "datasets\majors.json")  { Copy-Item "datasets\majors.json"  $BackupDir }
Write-Host "Backed up datasets to $BackupDir" -ForegroundColor Green
Write-Host ""

if ($DryRun) {
    Write-Host "[dry-run] Validating existing data..." -ForegroundColor Yellow
    python -m coursemap.ingestion.repair_dataset
    python -m coursemap.ingestion.patch_elective_gaps
    python -m coursemap.validation.dataset_validator --report
    Write-Host "[dry-run] Done - no files modified." -ForegroundColor Yellow
    exit 0
}

if (-not $PrereqOnly) {
    Write-Host "Step 1/5: Scraping courses from massey.ac.nz (~10-20 min)..." -ForegroundColor Yellow
    python -m coursemap.ingestion.build_dataset --output datasets/courses.json
    Write-Host "  Done." -ForegroundColor Green
    Write-Host ""

    Write-Host "Step 2/5: Scraping major requirement trees (~5-10 min)..." -ForegroundColor Yellow
    python -m coursemap.ingestion.build_majors_dataset --output datasets/majors.json
    Write-Host "  Done." -ForegroundColor Green
    Write-Host ""
}

if (-not $SkipPrereq) {
    Write-Host "Step 3/5: Scraping prerequisite AND/OR logic (~15-30 min)..." -ForegroundColor Yellow
    Write-Host "  (fetches each course page individually - this is the slow step)" -ForegroundColor Gray
    python -m coursemap.ingestion.refresh_prerequisites --concurrency 8
    Write-Host "  Done." -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host "Step 3/5: Skipping prerequisite scrape (-SkipPrereq)" -ForegroundColor Gray
    Write-Host ""
}

Write-Host "Step 4/5: Repairing data + patching free-elective gaps..." -ForegroundColor Yellow
python -m coursemap.ingestion.repair_dataset
python -m coursemap.ingestion.patch_elective_gaps
Write-Host "  Done." -ForegroundColor Green

# Clear the plan cache so stale plans aren't served after a dataset update
if (Test-Path "data\plans.db") {
    Remove-Item "data\plans.db" -Force
    Write-Host "  Cleared plan cache (data\plans.db)" -ForegroundColor Gray
}
Write-Host ""

Write-Host "Step 5/5: Validating + running tests..." -ForegroundColor Yellow
python -m coursemap.validation.dataset_validator --report
python -m pytest tests/ -q --tb=short
Write-Host "  Done." -ForegroundColor Green
Write-Host ""

Write-Host "=== Refresh complete! ===" -ForegroundColor Cyan
Write-Host "Review changes: git diff --stat datasets/" -ForegroundColor Gray
Write-Host ""
