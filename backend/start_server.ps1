# Wings Theory — server launcher.
#
# Starts the backend bound to all network interfaces so tablets on the shop
# Wi-Fi (or a phone hotspot) can reach it, and keeps the window open on a
# crash so an error is visible instead of the icon just vanishing.
#
# A phone hotspot in particular can hand this PC a *different* address every
# time it reconnects — the exact thing that made a tablet suddenly unable to
# reach a server that was actually running fine. So this window doesn't just
# print the address once: it keeps watching, and reprints in yellow the
# moment the address changes, while the server keeps running underneath.
#
# Run directly:      right-click -> Run with PowerShell
# Run at Windows login: place a shortcut to this file in
#   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Production database path and environment, made explicit rather than left
# to wt/db.py's WT_DB default — see BASELINE_SNAPSHOT.json's audit: dev,
# test and prod previously all resolved to the same implicit path with
# nothing distinguishing them. This launcher now names its target file
# outright and marks itself as production, which turns on the startup
# integrity gate and the default-secret refusal in wt/db.py
# (validate_production_startup) — neither fires for a dev run that never
# sets WT_ENV.
$env:WT_DB = Join-Path $PSScriptRoot "wings_theory.db"
$env:WT_ENV = "production"

# WT_SECRET signs every login token. Generating a new one on every launch
# would silently log everyone out on every restart (HS256 tokens signed
# under the old secret stop verifying), so it is generated once and
# persisted locally rather than regenerated per run. backend/.secret is
# gitignored — this file must never be committed.
$secretPath = Join-Path $PSScriptRoot ".secret"
if (-not (Test-Path $secretPath)) {
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [Convert]::ToBase64String($bytes) | Set-Content -Path $secretPath -Encoding utf8 -NoNewline
    Write-Host "Generated a new signing secret at $secretPath -- back this up; losing it logs everyone out." -ForegroundColor Yellow
}
$env:WT_SECRET = Get-Content -Path $secretPath -Raw

function Get-LanIp {
    $defaultRoute = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($defaultRoute) {
        $addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $defaultRoute.InterfaceAlias `
            -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($addr) { return $addr.IPAddress }
    }
    return "127.0.0.1"
}
function Show-Urls([string]$ip, [string]$color) {
    Write-Host "Console (owner):   http://${ip}:8000/console" -ForegroundColor $color
    Write-Host "POS terminal:      http://${ip}:8000/pos" -ForegroundColor $color
    Write-Host "Kitchen display:   http://${ip}:8000/kds" -ForegroundColor $color
}

$lanIp = Get-LanIp
Write-Host "Wings Theory -- starting backend (production)..." -ForegroundColor Yellow
Write-Host "Database: $env:WT_DB"
Show-Urls $lanIp "White"
Write-Host "(tablets must be on the same Wi-Fi / hotspot as this PC)"
Write-Host ""

$proc = $null
try {
    $proc = Start-Process -FilePath "python" `
        -ArgumentList "-m", "uvicorn", "wt.api:app", "--host", "0.0.0.0", "--port", "8000" `
        -NoNewWindow -PassThru

    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 10
        $currentIp = Get-LanIp
        if ($currentIp -ne $lanIp) {
            $lanIp = $currentIp
            Write-Host ""
            Write-Host "Network address changed. If a tablet stops connecting, use the new address below:" -ForegroundColor Yellow
            Show-Urls $lanIp "Yellow"
            Write-Host ""
        }
    }
} catch {
    Write-Host ""
    Write-Host "Server stopped with an error:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
} finally {
    if ($proc -and -not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Press any key to close this window..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
