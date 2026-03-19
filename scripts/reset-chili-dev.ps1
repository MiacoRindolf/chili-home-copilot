# Hard reset: stop Brain Worker + free port 8000 (when UI/API stop fails).
# Re-launches as Administrator if needed to kill protected processes.
#
# Run in YOUR Windows PowerShell (outside Cursor if needed), from anywhere:
#   powershell -ExecutionPolicy Bypass -File C:\dev\chili-home-copilot\scripts\reset-chili-dev.ps1
#
# Optional: also launch HTTPS uvicorn on port 8000:
#   .\scripts\reset-chili-dev.ps1 -StartUvicorn

param(
    [switch]$StartUvicorn
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Self-elevate if not Administrator (enables killing protected processes / cross-session)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Re-launching as Administrator (UAC). A NEW PowerShell window will run the reset - watch that window for 'Done' or errors; this one may only show the UAC step." -ForegroundColor Yellow
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath)
    if ($StartUvicorn) { $argList += "-StartUvicorn" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList
    Write-Host "This window is done. Open the elevated window that appeared - it will print 'Done' when finished." -ForegroundColor Cyan
    exit
}

Set-Location $ProjectRoot
$data = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null

Write-Host "=== CHILI dev reset (elevated) ===" -ForegroundColor Cyan

# 0) Stop scheduled tasks that might respawn brain worker
try {
    $tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -match 'chili|brain' -and $_.State -ne 'Disabled' }
    foreach ($t in $tasks) {
        Write-Host "Stopping and disabling scheduled task: $($t.TaskName)" -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
    }
} catch { Write-Host "ScheduledTask scan skipped: $_" -ForegroundColor DarkGray }

# 1) Graceful stop hint (worker deletes file when it sees it)
$stopFile = Join-Path $data "brain_worker_stop"
New-Item -ItemType File -Force -Path $stopFile | Out-Null
Write-Host "Wrote $stopFile (graceful stop hint)" -ForegroundColor Gray

# 2) Kill PID from status JSON
$statusFile = Join-Path $data "brain_worker_status.json"
if (Test-Path $statusFile) {
    try {
        $j = Get-Content -Raw $statusFile | ConvertFrom-Json
        if ($j.pid) {
            Write-Host "Stopping brain worker PID $($j.pid) from status file (tree)..." -ForegroundColor Yellow
            cmd /c "taskkill /F /T /PID $($j.pid)" 2>$null | Out-Null
            Stop-Process -Id $j.pid -Force -ErrorAction SilentlyContinue
        }
    } catch { Write-Host "Could not parse status JSON: $_" -ForegroundColor DarkYellow }
}

Start-Sleep -Milliseconds 500

# 3) Force-kill python processes for THIS repo only (avoid random conda/uvicorn)
$rootEsc = [regex]::Escape($ProjectRoot)
try {
    $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            if ($cmd -notmatch $rootEsc) { return $false }
            if ($cmd -match 'brain_worker\.py') { return $true }
            if ($cmd -match 'uvicorn' -and $cmd -match 'app\.main:app') { return $true }
            return $false
        }
    foreach ($p in $procs) {
        $snip = if ($p.CommandLine.Length -gt 100) { $p.CommandLine.Substring(0, 100) + "..." } else { $p.CommandLine }
        Write-Host "Killing PID $($p.ProcessId) (tree): $snip" -ForegroundColor Yellow
        cmd /c "taskkill /F /T /PID $($p.ProcessId)" 2>$null | Out-Null
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-Host "WMI process scan failed: $_" -ForegroundColor Red
}

Start-Sleep -Seconds 1

# 4) Free port 8000 (uses taskkill /T + self-elevates to Admin if needed)
& "$PSScriptRoot\free-port.ps1" -Port 8000
if ($LASTEXITCODE -ne 0) {
    Write-Host "Port 8000 could not be freed (HTTP.sys PID 4, Hyper-V excluded range, WSL/Docker, or protected process)." -ForegroundColor Red
    Write-Host "Run: .\scripts\diagnose-port-8000.ps1" -ForegroundColor Yellow
}

# 5) Drop lock so a new worker can start
Remove-Item (Join-Path $data "brain_worker.lock") -Force -ErrorAction SilentlyContinue

if ($StartUvicorn) {
    $cert = Join-Path $ProjectRoot "certs\localhost.pem"
    $key = Join-Path $ProjectRoot "certs\localhost.key"
    $cmd = "Set-Location '$ProjectRoot'; "
    if ((Test-Path $cert) -and (Test-Path $key)) {
        $cmd += "uvicorn app.main:app --host 0.0.0.0 --port 8000 --ssl-certfile '$cert' --ssl-keyfile '$key'"
    } else {
        $cmd += "uvicorn app.main:app --host 0.0.0.0 --port 8000"
    }
    Start-Process powershell -ArgumentList @("-NoExit", "-NoProfile", "-Command", $cmd)
    Write-Host "Started CHILI server on port 8000 in new window." -ForegroundColor Green
} else {
    Write-Host "Done. Start server yourself, or re-run with -StartUvicorn" -ForegroundColor Green
}
