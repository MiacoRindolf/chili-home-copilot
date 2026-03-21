# HTTP dev server: free a port, then uvicorn. Use when 8000 is stuck or in Windows excluded range.
#   $env:CHILI_PORT = '8010'   # try this port first
#   .\scripts\start-dev.ps1
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectRoot

$first = $null
if ($env:CHILI_PORT -match '^\d+$') { $first = [int]$env:CHILI_PORT }

$candidates = @()
if ($null -ne $first) { $candidates += $first }
$candidates += 8000, 8010, 8020, 8030, 8765

$chosen = $null
foreach ($p in ($candidates | Select-Object -Unique)) {
    & "$PSScriptRoot\free-port.ps1" -Port $p
    if ($LASTEXITCODE -eq 0) {
        $chosen = $p
        break
    }
    Write-Host "(Port $p not usable, trying next...)" -ForegroundColor DarkGray
}

if ($null -eq $chosen) {
    Write-Host "No usable port found. Run: .\scripts\diagnose-port-8000.ps1" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== CHILI (HTTP) on port $chosen ===" -ForegroundColor Green
Write-Host "  http://127.0.0.1:${chosen}/chat" -ForegroundColor Cyan
Write-Host "  Brain: http://127.0.0.1:${chosen}/brain" -ForegroundColor Cyan
Write-Host "  (No TLS — open http:// not https:// or you get PR_END_OF_FILE_ERROR in Firefox.)" -ForegroundColor Yellow
Write-Host ""

python -m uvicorn app.main:app --reload --host 0.0.0.0 --port $chosen
