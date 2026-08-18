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
Write-Host "Wings Theory -- starting backend..." -ForegroundColor Yellow
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
