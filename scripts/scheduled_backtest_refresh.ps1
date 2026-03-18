# Scheduled Backtest Refresh Script
# Runs the Python refresh script with proper environment

$projectPath = "c:\dev\chili-home-copilot"
$logFile = "$projectPath\backtest_refresh_scheduled.log"

Set-Location $projectPath

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logFile -Value "=== Scheduled refresh started at $timestamp ==="

try {
    python scripts/refresh_all_backtests.py 2>&1 | Tee-Object -Append -FilePath $logFile
    $exitCode = $LASTEXITCODE
    $endTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "=== Completed at $endTime with exit code $exitCode ==="
} catch {
    Add-Content -Path $logFile -Value "ERROR: $_"
}
