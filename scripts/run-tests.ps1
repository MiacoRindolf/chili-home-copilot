# Usage: .\scripts\run-tests.ps1 [pytest-args]
# Outputs:
#   tests-summary.txt  - structured summary (exit code, duration, pass/fail counts)
#   tests-output.log   - full pytest stdout+stderr
# Exit code: pytest's exit code, or 124 on timeout

param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
$startTime = Get-Date

if (-not $PytestArgs) {
    $PytestArgs = @("tests/", "-v", "--tb=short", "-rs", "--timeout=120", "-p", "no:cacheprovider")
}

$env:PYTHONUNBUFFERED = "1"
$env:CHILI_PYTEST = "1"

# Kill any leftover pytest from prior runs
Get-Process python -ErrorAction SilentlyContinue | Where-Object {
    try { $_.CommandLine -like "*pytest*" } catch { $false }
} | Stop-Process -Force -ErrorAction SilentlyContinue

$timeoutSeconds = 1800
$logFile = "tests-output.log"
$errFile = "tests-output.err.log"
$summaryFile = "tests-summary.txt"

Remove-Item -Path $logFile, $errFile, $summaryFile -ErrorAction SilentlyContinue

$condaArgs = @("run", "-n", "chili-env", "python", "-u", "-m", "pytest") + $PytestArgs

Write-Host "Running: conda $($condaArgs -join ' ')"
$proc = Start-Process -FilePath "conda" -ArgumentList $condaArgs -RedirectStandardOutput $logFile -RedirectStandardError $errFile -PassThru -NoNewWindow

if (-not $proc.WaitForExit($timeoutSeconds * 1000)) {
    Write-Host "TIMEOUT: pytest exceeded $timeoutSeconds seconds"
    try { $proc.Kill() } catch {}
    $exitCode = 124
    $duration = (Get-Date) - $startTime
    @"
duration_seconds: $([math]::Round($duration.TotalSeconds, 1))
exit_code: $exitCode
pytest_summary: TIMEOUT after $($duration.TotalSeconds) seconds (limit: $timeoutSeconds)
log_file: $logFile
"@ | Set-Content $summaryFile
    Write-Host "=== TEST RUN COMPLETE (TIMEOUT) ==="
    Get-Content $summaryFile
    exit $exitCode
}

$exitCode = $proc.ExitCode
$duration = (Get-Date) - $startTime

if (Test-Path $errFile) {
    Get-Content $errFile | Add-Content $logFile
    Remove-Item $errFile
}

$logContent = if (Test-Path $logFile) { Get-Content $logFile -Raw } else { "" }

# conda.exe on Windows often leaves [Process].ExitCode unset; infer from pytest output.
if ($null -eq $exitCode) {
    if ($logContent -match '(?m)\b\d+\s+failed\b') {
        $exitCode = 1
    } elseif ($logContent -match '(?m)^=+\s*\d+\s+error') {
        $exitCode = 1
    } elseif ($logContent -match '(?m)\b\d+\s+passed\b') {
        $exitCode = 0
    } else {
        $exitCode = 1
    }
}

# Final pytest banner line only (avoid matching "error" inside "UserWarning", etc.)
$pytestSummary = "no summary line found"
if ($logContent) {
    $bannerLines = @(
        $logContent -split "`r?`n" | Where-Object {
            $_ -match '^\s*=+\s*.+\s*=+\s*$' -and ($_ -match '\d+\s+passed|\d+\s+failed|\d+\s+errors?\b|\d+\s+skipped')
        }
    )
    if ($bannerLines.Count -gt 0) {
        $pytestSummary = $bannerLines[-1].Trim().Trim('=').Trim()
    }
}

@"
duration_seconds: $([math]::Round($duration.TotalSeconds, 1))
exit_code: $exitCode
pytest_summary: $pytestSummary
log_file: $logFile
"@ | Set-Content $summaryFile

Write-Host ""
Write-Host "=== TEST RUN COMPLETE ==="
Get-Content $summaryFile
Write-Host "========================="

exit $exitCode
