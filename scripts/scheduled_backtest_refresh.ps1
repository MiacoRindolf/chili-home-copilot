# Scheduled Backtest Refresh Script
# Runs a bounded, low-impact backtest refresh. Keep this conservative:
# this task runs on the operator PC, not a dedicated batch host.


# ── MARKET-WINDOW GUARD (added 2026-06-11): the momentum lane now trades the FULL
# US data session (premarket 4:00 AM ET -> after-hours 8:00 PM ET = 01:00-17:00 PT
# on this box). Heavy DB/CPU work in that window contends with LIVE trading (this
# task's old slot landed mid-premarket / pre-open). Inside the window: defer — the
# CHILI-Evening-* companion task runs this same script after the session closes.
$__nowLocal = Get-Date
if ($__nowLocal.DayOfWeek -ne 'Saturday' -and $__nowLocal.DayOfWeek -ne 'Sunday') {
    $__mod = $__nowLocal.Hour * 60 + $__nowLocal.Minute
    if ($__mod -ge 60 -and $__mod -lt 1020) {
        Write-Output "[market-window-guard] deferred (PT $($__nowLocal.ToString('HH:mm')) inside data session); evening task covers this."
        exit 0
    }
}
$ErrorActionPreference = "Stop"
$projectPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logFile = "$projectPath\backtest_refresh_scheduled.log"
$python = "C:\Users\rindo\miniconda3\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

Set-Location $projectPath

function Rotate-Log {
    param([string]$Path, [int64]$MaxBytes = 20971520)
    if ((Test-Path $Path) -and ((Get-Item $Path).Length -gt $MaxBytes)) {
        $old = "$Path.1"
        if (Test-Path $old) {
            Remove-Item -LiteralPath $old -Force
        }
        Move-Item -LiteralPath $Path -Destination $old
    }
}

Rotate-Log -Path $logFile

$env:TQDM_DISABLE = "1"
$env:CHILI_APP_NAME = "chili-backtest-refresh"
if (-not $env:CHILI_BACKTEST_REFRESH_WORKERS) {
    $env:CHILI_BACKTEST_REFRESH_WORKERS = "2"
}

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "=== Scheduled refresh started at $timestamp ==="

try {
    # *> redirection under $ErrorActionPreference="Stop" turns the first
    # native stderr line (an [INFO] log from the refresher) into a
    # terminating error, which killed the refresh ~30s after start every
    # run. Relax EAP for the native call only; the exit code is checked.
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $python scripts/refresh_all_backtests.py `
            --limit 1 `
            --target-tickers 8 `
            --workers $env:CHILI_BACKTEST_REFRESH_WORKERS `
            --max-runtime-minutes 45 `
            --sleep-seconds 10 *> $null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $endTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "=== Completed at $endTime with exit code $exitCode ==="
    if ($exitCode -ne 0) {
        exit $exitCode
    }
} catch {
    Add-Content -Path $logFile -Value "ERROR: $_"
    exit 1
}
