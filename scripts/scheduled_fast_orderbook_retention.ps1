# Scheduled Fast Orderbook Retention Maintenance
# Runs a bounded, low-impact purge of old fast_orderbook_default rows.
# Keep this conservative: this task runs on the operator PC, not a
# dedicated batch host.

$ErrorActionPreference = "Stop"
$projectPath = "c:\dev\chili-home-copilot"
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

    $pgReadyArgs = @("compose", "exec", "-T", "postgres", "pg_isready", "-U", "chili", "-d", "chili")
    & docker @pgReadyArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        Add-RunLog "postgres is not ready; skipping"
        exit 0
    }

    $maintenanceArgs = @(
        "compose", "exec", "-T", "chili",
        "python", "/workspace/scripts/maintain_fast_orderbook_retention.py",
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
