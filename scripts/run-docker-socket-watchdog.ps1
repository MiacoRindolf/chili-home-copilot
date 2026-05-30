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
    [string]$ComposeProjectName = "chili-home-copilot",
    [int]$ComposeCollisionEventLookbackMinutes = 20,
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

function Normalize-PathForCompare {
    param([string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ""
    }
    return $Path.Replace("/", "\").TrimEnd("\").ToLowerInvariant()
}

function Resolve-CanonicalRoot {
    param([string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $gitCommand) {
        return $resolved
    }

    try {
        $gitResult = Invoke-NativeCapture -FilePath "git" -Arguments @(
            "-C", $resolved, "rev-parse", "--show-toplevel"
        )
        if ($gitResult.exit_code -eq 0 -and $gitResult.output.Count -gt 0) {
            $top = [string]$gitResult.output[0]
            if (-not [string]::IsNullOrWhiteSpace($top)) {
                return $top.Replace("/", "\")
            }
        }
    } catch {
        return $resolved
    }

    return $resolved
}

function Get-ComposeCollisionSummary {
    param(
        [string]$ExpectedRoot,
        [string]$ProjectName,
        [int]$EventLookbackMinutes
    )

    $expected = Normalize-PathForCompare -Path $ExpectedRoot
    $summary = [ordered]@{
        available = $false
        project_name = $ProjectName
        expected_working_dir = $ExpectedRoot
        current_collision_count = 0
        recent_event_collision_count = 0
        current_collisions = @()
        recent_event_collisions = @()
        error = $null
    }

    if ([string]::IsNullOrWhiteSpace($ProjectName)) {
        $summary.error = "compose_project_name_missing"
        return $summary
    }

    try {
        $idResult = Invoke-NativeCapture -FilePath "docker" -Arguments @(
            "ps", "-a",
            "--filter", "label=com.docker.compose.project=$ProjectName",
            "--format", "{{.ID}}"
        )
        if ($idResult.exit_code -ne 0) {
            $summary.error = ($idResult.output -join "`n")
            return $summary
        }

        $ids = @($idResult.output | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($ids.Count -gt 0) {
            $inspectArgs = @("inspect") + $ids
            $inspectResult = Invoke-NativeCapture -FilePath "docker" -Arguments $inspectArgs
            if ($inspectResult.exit_code -eq 0) {
                $objects = @()
                ($inspectResult.output -join "`n") |
                    ConvertFrom-Json |
                    ForEach-Object { $objects += $_ }
                $currentCollisions = @()
                foreach ($object in $objects) {
                    $labels = $object.Config.Labels
                    $workingDir = [string]$labels."com.docker.compose.project.working_dir"
                    if ((Normalize-PathForCompare -Path $workingDir) -eq $expected) {
                        continue
                    }
                    $currentCollisions += [pscustomobject]@{
                        container = ([string]$object.Name).TrimStart("/")
                        service = [string]$labels."com.docker.compose.service"
                        working_dir = $workingDir
                        state = [string]$object.State.Status
                    }
                }
                $summary.current_collisions = $currentCollisions
                $summary.current_collision_count = $currentCollisions.Count
            }
        }

        $lookback = [math]::Max(1, $EventLookbackMinutes)
        $eventResult = Invoke-NativeCapture -FilePath "docker" -Arguments @(
            "events",
            "--since", ("{0}m" -f $lookback),
            "--until", "0s",
            "--filter", "type=container",
            "--filter", "event=create",
            "--filter", "event=destroy",
            "--filter", "event=rename",
            "--format", "{{json .}}"
        )
        if ($eventResult.exit_code -eq 0) {
            $eventCollisions = @()
            foreach ($line in @($eventResult.output | Select-Object -Last 200)) {
                if ([string]::IsNullOrWhiteSpace($line)) {
                    continue
                }
                try {
                    $event = $line | ConvertFrom-Json
                } catch {
                    continue
                }
                $attrs = $event.Actor.Attributes
                if ([string]$attrs."com.docker.compose.project" -ne $ProjectName) {
                    continue
                }
                $workingDir = [string]$attrs."com.docker.compose.project.working_dir"
                if ((Normalize-PathForCompare -Path $workingDir) -eq $expected) {
                    continue
                }
                $eventCollisions += [pscustomobject]@{
                    action = [string]$event.Action
                    container = [string]$attrs.name
                    service = [string]$attrs."com.docker.compose.service"
                    working_dir = $workingDir
                    time = $event.time
                }
            }
            $summary.recent_event_collisions = $eventCollisions
            $summary.recent_event_collision_count = $eventCollisions.Count
        } elseif ([string]::IsNullOrWhiteSpace([string]$summary.error)) {
            $summary.error = ($eventResult.output -join "`n")
        }

        $summary.available = $true
        return $summary
    } catch {
        $summary.error = $_.Exception.Message
        return $summary
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
    $resolvedRoot = Resolve-CanonicalRoot -Path $rootToResolve
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
    $composeCollisions = [ordered]@{
        available = $false
        project_name = $ComposeProjectName
        expected_working_dir = $resolvedRoot
        current_collision_count = 0
        recent_event_collision_count = 0
        current_collisions = @()
        recent_event_collisions = @()
        error = $null
    }
    if ($engineReachable) {
        $composeCollisions = Get-ComposeCollisionSummary `
            -ExpectedRoot $resolvedRoot `
            -ProjectName $ComposeProjectName `
            -EventLookbackMinutes $ComposeCollisionEventLookbackMinutes
    }
    $socketCount = $socketSummary.docker_bound_count
    $socketCritical = $false
    $socketWarning = $false
    if ($socketSummary.available -and $null -ne $socketCount) {
        $socketCritical = ([int]$socketCount -ge $CriticalDockerBoundSockets)
        $socketWarning = ([int]$socketCount -ge $WarnBoundSockets)
    }

    $severity = "info"
    $composeCollisionCritical = (
        ([int]$composeCollisions.current_collision_count -gt 0) -or
        ([int]$composeCollisions.recent_event_collision_count -gt 0)
    )

    if (-not $engineReachable -or $socketCritical -or $composeCollisionCritical) {
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
        compose = $composeCollisions
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
