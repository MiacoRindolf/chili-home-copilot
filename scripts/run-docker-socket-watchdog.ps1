<#
.SYNOPSIS
  Records Docker socket and engine health evidence for CHILI.

.DESCRIPTION
  This watchdog is intentionally observability-only. It does not stop, kill,
  restart, prune, remove, exec into, migrate, or otherwise mutate containers,
  volumes, databases, broker state, or live trading state.

  The existing Windows scheduled task calls this script with socket thresholds.
  This script satisfies that task safely by writing structured evidence that can
  explain Docker Desktop or socket pressure incidents. Stopped live services are
  handled by scripts/watch-live-runtime.ps1, not by this script.
#>
[CmdletBinding()]
param(
    [string]$Root,
    [int]$WarnBoundSockets = 2000,
    [int]$CriticalDockerBoundSockets = 8000,
    [int]$MaxProcessRows = 25,
    [switch]$Json,
    [string]$LogPath
)

$ErrorActionPreference = "Stop"

$ScriptDirectory = if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
    $PSScriptRoot
} else {
    Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $oldErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& $FilePath @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }

    return [pscustomobject]@{
        exit_code = $exitCode
        output = $output
    }
}

function Limit-Lines {
    param(
        [object[]]$Lines,
        [int]$MaxLines = 40,
        [int]$MaxCharsPerLine = 500
    )

    $result = @()
    foreach ($line in @($Lines | Select-Object -First $MaxLines)) {
        $text = [string]$line
        if ($text.Length -gt $MaxCharsPerLine) {
            $text = $text.Substring(0, $MaxCharsPerLine) + "...[truncated]"
        }
        $result += $text
    }
    return $result
}

function Get-DockerRelatedProcesses {
    $patterns = @(
        "docker",
        "com.docker",
        "dockerd",
        "vpnkit",
        "wsl",
        "wslhost",
        "wslrelay",
        "wslservice",
        "vmcompute",
        "vmmem"
    )

    $processes = @()
    foreach ($process in @(Get-Process -ErrorAction SilentlyContinue)) {
        $name = ([string]$process.ProcessName).ToLowerInvariant()
        $matched = $false
        foreach ($pattern in $patterns) {
            if ($name -like "*$pattern*") {
                $matched = $true
                break
            }
        }

        if (-not $matched) {
            continue
        }

        $processes += [pscustomobject]@{
            id = $process.Id
            name = $process.ProcessName
            handles = $process.Handles
            working_set_mb = [math]::Round(($process.WorkingSet64 / 1MB), 1)
            start_time = try { $process.StartTime.ToUniversalTime().ToString("o") } catch { $null }
        }
    }

    return @($processes | Sort-Object name, id)
}

function Get-SocketSummary {
    param([object[]]$DockerProcesses)

    $processNamesById = @{}
    foreach ($process in $DockerProcesses) {
        $processNamesById[[int]$process.id] = [string]$process.name
    }

    try {
        $connections = @(Get-NetTCPConnection -ErrorAction Stop)
    } catch {
        return [pscustomobject]@{
            available = $false
            error = $_.Exception.Message
            total_count = $null
            docker_bound_count = $null
            by_process = @()
            by_state = @()
        }
    }

    $dockerConnections = @(
        $connections |
            Where-Object { $processNamesById.ContainsKey([int]$_.OwningProcess) }
    )

    $byProcess = @(
        $dockerConnections |
            Group-Object OwningProcess |
            Sort-Object Count -Descending |
            Select-Object -First 20 |
            ForEach-Object {
                $ownerPid = [int]$_.Name
                [pscustomobject]@{
                    pid = $ownerPid
                    name = $processNamesById[$ownerPid]
                    count = $_.Count
                }
            }
    )

    $byState = @(
        $dockerConnections |
            Group-Object State |
            Sort-Object Count -Descending |
            ForEach-Object {
                [pscustomobject]@{
                    state = [string]$_.Name
                    count = $_.Count
                }
            }
    )

    return [pscustomobject]@{
        available = $true
        error = $null
        total_count = $connections.Count
        docker_bound_count = $dockerConnections.Count
        by_process = $byProcess
        by_state = $byState
    }
}

function Test-NamedPipe {
    param([string]$Path)

    try {
        return [bool](Test-Path -LiteralPath $Path -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Write-Summary {
    param([object]$Summary)

    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        try {
            $logDir = Split-Path -Parent $LogPath
            if (-not [string]::IsNullOrWhiteSpace($logDir)) {
                New-Item -ItemType Directory -Force -Path $logDir | Out-Null
            }
            $Summary | ConvertTo-Json -Depth 8 -Compress | Add-Content -LiteralPath $LogPath -Encoding ASCII
        } catch {
            if ($Summary -is [System.Collections.IDictionary]) {
                $Summary["log_write_error"] = $_.Exception.Message
            }
        }
    }

    if ($Json) {
        $Summary | ConvertTo-Json -Depth 8
        return
    }

    Write-Output ("[docker-socket-watchdog] severity={0} engine_reachable={1} docker_bound_sockets={2} pipe_exists={3}" -f `
        $Summary.severity,
        $Summary.docker.engine_reachable,
        $Summary.sockets.docker_bound_count,
        $Summary.docker.pipe_exists)
}

try {
    $rootToResolve = if ([string]::IsNullOrWhiteSpace($Root)) {
        Join-Path $ScriptDirectory ".."
    } else {
        $Root
    }
    $resolvedRoot = (Resolve-Path -LiteralPath $rootToResolve).Path
    if ([string]::IsNullOrWhiteSpace($LogPath)) {
        $LogPath = Join-Path $resolvedRoot "logs\docker-socket-watchdog.jsonl"
    }

    $dockerProcesses = @(Get-DockerRelatedProcesses)
    $socketSummary = Get-SocketSummary -DockerProcesses $dockerProcesses

    $pipePaths = @(
        "\\.\pipe\dockerDesktopLinuxEngine",
        "\\.\pipe\docker_engine"
    )
    $pipes = @(
        foreach ($pipePath in $pipePaths) {
            [pscustomobject]@{
                path = $pipePath
                exists = Test-NamedPipe -Path $pipePath
            }
        }
    )
    $pipeExists = [bool](@($pipes | Where-Object { $_.exists }).Count -gt 0)

    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    $dockerInfo = [pscustomobject]@{
        exit_code = $null
        output = @()
        skipped_reason = $null
    }
    $dockerPs = [pscustomobject]@{
        exit_code = $null
        output = @()
        skipped_reason = $null
    }

    if ($null -eq $dockerCommand) {
        $dockerInfo.skipped_reason = "docker_command_not_found"
        $dockerPs.skipped_reason = "docker_command_not_found"
    } elseif (-not $pipeExists) {
        $dockerInfo.skipped_reason = "docker_named_pipe_missing"
        $dockerPs.skipped_reason = "docker_named_pipe_missing"
    } else {
        $psResult = Invoke-NativeCapture -FilePath "docker" -Arguments @(
            "ps", "-a", "--format", "{{.Names}}`t{{.State}}`t{{.Status}}"
        )
        $dockerPs.exit_code = $psResult.exit_code
        $dockerPs.output = @(Limit-Lines -Lines $psResult.output -MaxLines 80)

        $infoResult = Invoke-NativeCapture -FilePath "docker" -Arguments @(
            "info", "--format", "{{json .ServerVersion}}"
        )
        $dockerInfo.exit_code = $infoResult.exit_code
        $dockerInfo.output = @(Limit-Lines -Lines $infoResult.output -MaxLines 10)
    }

    $engineReachable = ($dockerPs.exit_code -eq 0 -or $dockerInfo.exit_code -eq 0)
    $socketCount = $socketSummary.docker_bound_count
    $socketCritical = $false
    $socketWarning = $false
    if ($socketSummary.available -and $null -ne $socketCount) {
        $socketCritical = ([int]$socketCount -ge $CriticalDockerBoundSockets)
        $socketWarning = ([int]$socketCount -ge $WarnBoundSockets)
    }

    $severity = "info"
    if (-not $engineReachable -or $socketCritical) {
        $severity = "critical"
    } elseif ($socketWarning) {
        $severity = "warning"
    }

    $summary = [ordered]@{
        task = "docker_socket_watchdog"
        ok = ($severity -ne "critical")
        severity = $severity
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        root = $resolvedRoot
        remediation = "none_observability_only"
        thresholds = [ordered]@{
            warn_bound_sockets = $WarnBoundSockets
            critical_docker_bound_sockets = $CriticalDockerBoundSockets
        }
        docker = [ordered]@{
            command_available = ($null -ne $dockerCommand)
            pipe_exists = $pipeExists
            pipes = $pipes
            engine_reachable = $engineReachable
            info = $dockerInfo
            ps = $dockerPs
        }
        processes = [ordered]@{
            docker_related_count = $dockerProcesses.Count
            docker_related_sample_limit = $MaxProcessRows
            docker_related_sample = @(
                $dockerProcesses |
                    Sort-Object handles -Descending |
                    Select-Object -First $MaxProcessRows
            )
        }
        sockets = $socketSummary
    }

    Write-Summary -Summary $summary
    exit 0
} catch {
    $fallbackLogPath = $LogPath
    if ([string]::IsNullOrWhiteSpace($fallbackLogPath)) {
        $fallbackRoot = if ([string]::IsNullOrWhiteSpace($Root)) {
            Join-Path $ScriptDirectory ".."
        } else {
            $Root
        }
        $fallbackLogPath = Join-Path (Resolve-Path -LiteralPath $fallbackRoot).Path "logs\docker-socket-watchdog.jsonl"
    }

    $summary = [ordered]@{
        task = "docker_socket_watchdog"
        ok = $false
        severity = "critical"
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        remediation = "none_observability_only"
        error = $_.Exception.Message
    }

    $LogPath = $fallbackLogPath
    Write-Summary -Summary $summary
    exit 0
}
