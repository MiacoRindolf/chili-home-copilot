param(
    [string]$TaskName = "CHILI Docker Socket Watchdog",
    [int]$IntervalMinutes = 5,
    [int]$WarnBoundSockets = 2000,
    [int]$CriticalDockerBoundSockets = 8000,
    [string]$LogPath = "",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    $scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $PSCommandPath }
    return (Resolve-Path -LiteralPath (Join-Path $scriptDir "..")).Path
}

function Quote-TaskArg([string]$value) {
    if ($value -match '[\s"]') {
        return '"' + ($value -replace '"', '\"') + '"'
    }
    return $value
}

if ($IntervalMinutes -lt 1) {
    throw "IntervalMinutes must be at least 1."
}

$repoRoot = Get-RepoRoot
$repairScript = Join-Path $repoRoot "scripts\repair-docker-socket-exhaustion.ps1"
$watchdogScript = Join-Path $repoRoot "scripts\run-docker-socket-watchdog.ps1"
if (-not (Test-Path -LiteralPath $repairScript)) {
    throw "Repair script not found at $repairScript"
}
if (-not (Test-Path -LiteralPath $watchdogScript)) {
    throw "Watchdog script not found at $watchdogScript"
}

$customLogPath = -not [string]::IsNullOrWhiteSpace($LogPath)
if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $repoRoot "scripts\watcher-out\docker-socket-watchdog.log"
}
$logDir = Split-Path -Parent $LogPath
if (-not [string]::IsNullOrWhiteSpace($logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

$taskArgs = @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Quote-TaskArg $watchdogScript),
    "-WarnBoundSockets",
    [string]$WarnBoundSockets,
    "-CriticalDockerBoundSockets",
    [string]$CriticalDockerBoundSockets
) -join " "
if ($customLogPath) {
    $taskArgs = "$taskArgs -LogPath $(Quote-TaskArg $LogPath)"
}

function Register-WithScheduledTasks {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger `
        -Once `
        -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 25) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Checks for Docker Desktop bound-socket exhaustion and safely restarts Docker/Compose when the critical threshold is reached." `
        | Out-Null
}

function Register-WithSchTasks {
    $taskRun = "powershell.exe $taskArgs"
    & schtasks.exe /Create /TN $TaskName /SC MINUTE /MO $IntervalMinutes /TR $taskRun /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks.exe failed with exit code $LASTEXITCODE"
    }
}

try {
    if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
        Register-WithScheduledTasks
    } else {
        Register-WithSchTasks
    }
} catch {
    Write-Warning "ScheduledTasks registration failed: $_"
    Write-Host "Retrying with schtasks.exe ..."
    Register-WithSchTasks
}

if ($RunNow) {
    try {
        if (Get-Command Start-ScheduledTask -ErrorAction SilentlyContinue) {
            Start-ScheduledTask -TaskName $TaskName
        } else {
            & schtasks.exe /Run /TN $TaskName | Out-Host
        }
    } catch {
        Write-Warning "Task registered, but immediate run failed: $_"
    }
}

Write-Host "Installed scheduled task '$TaskName'."
Write-Host "Interval: every $IntervalMinutes minute(s)"
Write-Host "Watchdog script: $watchdogScript"
Write-Host "Repair script: $repairScript"
Write-Host "Log path: $LogPath"
