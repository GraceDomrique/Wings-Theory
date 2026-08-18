# Wings Theory — development server launcher.
#
# Same server, deliberately different database. WT_ENV is left unset here
# (not "development" — just unset), which is what keeps this run out of the
# production startup gate in wt/db.py (validate_production_startup is a
# no-op unless WT_ENV=production) without inventing a second environment
# name nothing else in the codebase checks for.
#
# The point of this file existing at all: before this, there was no
# launcher that pointed anywhere other than backend/wings_theory.db, so a
# developer running the app locally to test a change was, by default,
# reading and writing the same file the shop's tablets would ever connect
# to. This launcher's only job is to make that impossible by construction —
# WT_DB below can never resolve to the production path.
#
# Run directly:  right-click -> Run with PowerShell
# (No LAN-address banner, no window-stays-open-on-crash behavior — this is
# for a developer at the keyboard, not a shop floor.)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$devDir = Join-Path $PSScriptRoot "dev"
if (-not (Test-Path $devDir)) {
    New-Item -ItemType Directory -Path $devDir | Out-Null
}
$env:WT_DB = Join-Path $devDir "wings_theory_dev.db"
# WT_SECRET intentionally left to wt/auth.py's own default here — this
# database never leaves the developer's machine, and WT_ENV is unset, so
# the production secret check in db.validate_production_startup does not
# apply to this run at all.

Write-Host "Wings Theory -- starting backend (development)..." -ForegroundColor Cyan
Write-Host "Database: $env:WT_DB"
Write-Host "(separate from the production database; safe to seed, reset, or delete)"
Write-Host ""

python -m uvicorn wt.api:app --host 127.0.0.1 --port 8000 --reload
