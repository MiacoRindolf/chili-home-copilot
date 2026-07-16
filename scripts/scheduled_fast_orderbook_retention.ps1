# Scheduled Fast Orderbook Retention Maintenance
# Runs a bounded, low-impact purge of old fast_orderbook_default rows.
# Keep this conservative: this task runs on the operator PC, not a
# dedicated batch host.


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
$logFile = "$projectPath\fast_orderbook_retention_scheduled.log"
$lockFile = "$projectPath\data\fast_orderbook_retention_scheduled.lock"

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

function Add-RunLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$timestamp] $Message"
}

function Get-EnvInt {
    param([string]$Name, [int]$Default, [int]$MinValue)
    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }
    $parsed = 0
    if (([int]::TryParse($raw, [ref]$parsed)) -and ($parsed -ge $MinValue)) {
        return $parsed
    }
    return $Default
}

function Get-EnvDecimalString {
    param([string]$Name, [string]$Default)
    $raw = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }
    $parsed = 0.0
    if ([double]::TryParse($raw, [ref]$parsed) -and ($parsed -ge 0)) {
        return $raw
    }
    return $Default
}

function Enter-Lock {
    $lockDir = Split-Path -Parent $lockFile
    if (-not (Test-Path $lockDir)) {
        New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    }
    if (Test-Path $lockFile) {
        $age = (Get-Date) - (Get-Item $lockFile).LastWriteTime
        if ($age.TotalHours -lt 2) {
            Add-RunLog "another retention run appears active; skipping"
            exit 0
        }
        Remove-Item -LiteralPath $lockFile -Force
    }
    try {
        New-Item -ItemType File -Path $lockFile -ErrorAction Stop | Out-Null
    } catch {
        Add-RunLog "could not acquire retention lock; skipping"
        exit 0
    }
}

function Exit-Lock {
    if (Test-Path $lockFile) {
        Remove-Item -LiteralPath $lockFile -Force
    }
}

Rotate-Log -Path $logFile
Enter-Lock

try {
    $batchSize = Get-EnvInt -Name "CHILI_FAST_ORDERBOOK_RETENTION_BATCH_SIZE" -Default 50000 -MinValue 1
    $maxBatches = Get-EnvInt -Name "CHILI_FAST_ORDERBOOK_RETENTION_MAX_BATCHES" -Default 4 -MinValue 1
    $maxRuntimeMinutes = Get-EnvInt -Name "CHILI_FAST_ORDERBOOK_RETENTION_MAX_RUNTIME_MINUTES" -Default 4 -MinValue 1
    $statementTimeoutMs = Get-EnvInt -Name "CHILI_FAST_ORDERBOOK_RETENTION_STATEMENT_TIMEOUT_MS" -Default 45000 -MinValue 1000
    $sleepSeconds = Get-EnvDecimalString -Name "CHILI_FAST_ORDERBOOK_RETENTION_SLEEP_SECONDS" -Default "1"

    Add-RunLog "scheduled retention starting batch=$batchSize max_batches=$maxBatches max_runtime_minutes=$maxRuntimeMinutes"

    # Deployed container names (raw `docker run` deploy model; the old
    # compose project is retired - `docker compose exec` here failed every
    # night with 'service "chili" is not running'). Override via env if a
    # deploy renames them.
    $postgresContainer = if ($env:CHILI_POSTGRES_CONTAINER) { $env:CHILI_POSTGRES_CONTAINER } else { "chili-home-copilot-postgres-1" }
    $appContainer = if ($env:CHILI_APP_CONTAINER) { $env:CHILI_APP_CONTAINER } else { "chili-clean-recovery-scheduler" }

    $pgReadyArgs = @("exec", $postgresContainer, "pg_isready", "-U", "chili", "-d", "chili")
    & docker @pgReadyArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        Add-RunLog "postgres is not ready; skipping"
        exit 0
    }

    $maintenanceArgs = @(
        "exec", $appContainer,
        "python", "/app/scripts/maintain_fast_orderbook_retention.py",
        "--execute",
        "--batch-size", "$batchSize",
        "--max-batches", "$maxBatches",
        "--max-runtime-minutes", "$maxRuntimeMinutes",
        "--sleep-seconds", "$sleepSeconds",
        "--statement-timeout-ms", "$statementTimeoutMs"
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $output = & docker @maintenanceArgs 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $output | Add-Content -Path $logFile

    $endTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "=== Completed at $endTime with exit code $exitCode ==="
    if ($exitCode -ne 0) {
        exit $exitCode
    }
} catch {
    Add-RunLog "ERROR: $_"
    exit 1
} finally {
    Exit-Lock
}
