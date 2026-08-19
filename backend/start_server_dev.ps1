# Wings Theory — development server launcher.
#
# Same server, deliberately different database and an explicit WT_ENV. Both
# WT_ENV and WT_DB are required now — wt/db.py's db_path() refuses to start
# at all if either is unset, and cross-checks the pairing: WT_ENV=development
# is refused outright if WT_DB were ever pointed at the production file.
# There is no implicit "leave WT_ENV unset and it behaves like dev" mode any
# more; unset is its own refusal, on purpose (see db.environment()).
#
# The point of this file existing at all: before this, there was no
# launcher that pointed anywhere other than backend/wings_theory.db, so a
# developer running the app locally to test a change was, by default,
# reading and writing the same file the shop's tablets would ever connect
# to. This launcher's only job is to make that impossible by construction —
# WT_DB below can never resolve to the production path, and WT_ENV=development
# makes db.reset() (seed --fresh) refuse unless WT_ALLOW_DESTRUCTIVE=1 is also
# set for that one command.
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
$env:WT_ENV = "development"

# A per-developer-machine secret, generated once and persisted locally
# rather than left as wt/auth.py's hardcoded DEFAULT_SECRET — that literal
# is checked into source and known to anyone who can read this repo, which
# is a fine threat model for "nobody but this developer ever reaches this
# port" but not a habit worth encouraging even in dev. backend/dev/.secret
# is gitignored, same as the production one.
$devSecretPath = Join-Path $devDir ".secret"
if (-not (Test-Path $devSecretPath)) {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -Path $devSecretPath -Encoding utf8 -NoNewline
    Write-Host "Generated a new dev signing secret at $devSecretPath" -ForegroundColor Cyan
}
$env:WT_SECRET = Get-Content -Path $devSecretPath -Raw

Write-Host "Wings Theory -- starting backend (development)..." -ForegroundColor Cyan
Write-Host "Database: $env:WT_DB"
Write-Host "(separate from the production database; safe to seed, reset, or delete)"
Write-Host ""

python -m uvicorn wt.api:app --host 127.0.0.1 --port 8000 --reload
