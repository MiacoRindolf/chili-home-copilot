# Usage: .\scripts\run-tests.ps1 [pytest-args]
# Outputs:
#   tests-summary.txt  - structured summary (exit code, duration, pass/fail counts)
#   tests-output.log   - full pytest stdout+stderr, streamed in real-time
# Exit code: pytest's exit code, or 124 on timeout

param(
    [Parameter(ValueFromRemainingArguments = $true)]
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

# 60-minute hard wrapper timeout (per-test timeout is 120s via pytest-timeout)
$timeoutSeconds = 3600
$logFile = "tests-output.log"
$summaryFile = "tests-summary.txt"

Remove-Item -Path $logFile, $summaryFile, "tests-output.err.log" -ErrorAction SilentlyContinue

# cmd.exe: one /c string so shell applies > file 2>&1 to the full conda+pytest chain (line-oriented flush)
function Format-CmdToken([string]$a) {
    if ($null -eq $a) { return '""' }
    if ($a -match '[^\w\.\-\/\\:()]' -or $a -eq "") {
        return '"' + ($a -replace '"', '""') + '"'
    }
    return $a
}

# Must use a resolved conda path: Start-Process cmd does not always honor the same PATH as the interactive shell.
$condaExe = (Get-Command conda.exe -ErrorAction Stop).Source
$runTokens = @($condaExe, "run", "--no-capture-output", "-n", "chili-env", "python", "-u", "-m", "pytest") + $PytestArgs
$pytestLine = ($runTokens | ForEach-Object { Format-CmdToken $_ }) -join " "
$logFull = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $logFile))
$innerCmd = "$pytestLine > `"$logFull`" 2>&1"

Write-Host "Running: $pytestLine"
Write-Host "Wrapper timeout: $timeoutSeconds seconds"
Write-Host "Streaming output to: $logFile"
Write-Host ""

$proc = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $innerCmd) -PassThru -NoNewWindow

# Watcher loop with heartbeat every 60 seconds, polling for completion
$lastHeartbeat = Get-Date
$heartbeatInterval = 60
$exitCode = $null
$timedOut = $false

while ($true) {
    if ($proc.HasExited) {
        $exitCode = $proc.ExitCode
        break
    }

    $elapsed = ((Get-Date) - $startTime).TotalSeconds
    if ($elapsed -ge $timeoutSeconds) {
        Write-Host "TIMEOUT: pytest exceeded $timeoutSeconds seconds, killing"
        try {
            # Terminate cmd and the full child tree (conda -> python -> pytest)
            & taskkill.exe /F /T /PID $proc.Id 2>$null | Out-Null
        } catch { }
        $exitCode = 124
        $timedOut = $true
        break
    }

    $sinceHeartbeat = ((Get-Date) - $lastHeartbeat).TotalSeconds
    if ($sinceHeartbeat -ge $heartbeatInterval) {
        $logSize = 0
        $lastLine = ""
        try {
            if (Test-Path $logFull) { $logSize = (Get-Item $logFull -ErrorAction Stop).Length }
            if (Test-Path $logFull) { $lastLine = Get-Content $logFull -Tail 1 -ErrorAction SilentlyContinue }
        } catch { }
        Write-Host "[$([math]::Round($elapsed, 0))s elapsed, log=${logSize}B] $lastLine"
        $lastHeartbeat = Get-Date
    }

    Start-Sleep -Seconds 5
}

$duration = (Get-Date) - $startTime
$logContent = ""
try {
    if (Test-Path $logFull) { $logContent = Get-Content $logFull -Raw -ErrorAction Stop }
} catch { }

# Parse final pytest session banner (same as legacy wrapper; avoids "error" in UserWarning)
$pytestSummary = "no summary line found"
if ($logContent) {
    $bannerLines = @(
        $logContent -split "`r?`n" | Where-Object {
            $_ -match '^\s*=+\s*.+\s*=+\s*$' -and ($_ -match '\d+\s+passed|\d+\s+failed|\d+\s+errors?\b|\d+\s+skipped')
        }
    )
    if ($bannerLines.Count -gt 0) {
        $pytestSummary = $bannerLines[-1].Trim()
    }
}

# conda/cmd on Windows can leave [Process].ExitCode unset; infer from log
if ($null -eq $exitCode) {
    if ($logContent -match '(?m)\b\d+\s+failed\b') { $exitCode = 1 }
    elseif ($logContent -match '(?m)\b\d+\s+error\b' -or $logContent -match '(?m)^=+\s*\d+\s+error') { $exitCode = 1 }
    elseif ($logContent -match '(?m)\b\d+\s+passed\b') { $exitCode = 0 }
    elseif ($pytestSummary -match '\d+\s+failed' -or $pytestSummary -match '\d+\s+error') { $exitCode = 1 }
    elseif ($pytestSummary -match '\d+\s+passed') { $exitCode = 0 }
    else { $exitCode = 1 }
}

@"
duration_seconds: $([math]::Round($duration.TotalSeconds, 1))
exit_code: $exitCode
pytest_summary: $pytestSummary
log_file: $logFile
timed_out: $timedOut
log_size_bytes: $(if (Test-Path $logFull) { (Get-Item $logFull).Length } else { 0 })
"@ | Set-Content $summaryFile

Write-Host ""
Write-Host "=== TEST RUN COMPLETE ==="
Get-Content $summaryFile
Write-Host "========================="

exit $exitCode
