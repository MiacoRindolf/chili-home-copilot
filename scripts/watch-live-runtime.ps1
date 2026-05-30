<#
.SYNOPSIS
  Starts stopped live-critical CHILI Compose services without touching data.

.DESCRIPTION
  Docker restart policies recover crashes, but they intentionally do not
  recover containers stopped by an explicit docker stop/kill/compose stop.
  This watchdog closes that operational gap for the live trading runtime.

  The script only calls `docker compose up -d` for foundation/live-critical
  services that are currently missing or not running. It does not run
  migrations, exec into containers, remove containers, recreate volumes, or
  start heavy/offline lanes.

  Maintenance escape hatches:
    * Set CHILI_LIVE_RUNTIME_WATCHDOG_DISABLED=1, or
    * Set CHILI_LIVE_RUNTIME_RESTART_POLICY=no in the environment or .env, or
    * Create .chili-live-runtime-maintenance in the repo root.
#>
[CmdletBinding()]
param(
    [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string[]]$Services = @(
        "postgres",
        "ollama",
        "chili",
        "broker-sync-worker",
        "autotrader-worker",
        "fast-scan-worker",
        "market-snapshot-worker"
    ),
    [string[]]$FoundationServices = @("postgres", "ollama"),
    [switch]$DryRun,
    [switch]$Json,
    [int]$WaitSeconds = 20,
    [string]$LogPath
)

$ErrorActionPreference = "Stop"

function Test-Truthy {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }

    return @("1", "true", "yes", "y", "on") -contains $Value.Trim().ToLowerInvariant()
}

function Read-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    $pattern = "^\s*" + [regex]::Escape($Name) + "\s*=\s*(.*)\s*$"
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }

        $match = [regex]::Match($trimmed, $pattern)
        if (-not $match.Success) {
            continue
        }

        $value = $match.Groups[1].Value.Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        return $value
    }

    return $null
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

function Get-ComposeRows {
    $result = Invoke-NativeCapture -FilePath "docker" -Arguments @(
        "compose", "ps", "-a", "--format", "json"
    )
    if ($result.exit_code -ne 0) {
        throw "docker compose ps failed: $($result.output -join "`n")"
    }

    $raw = @($result.output)
    $text = ($raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return @()
    }

    if ($text.StartsWith("[")) {
        return @($text | ConvertFrom-Json)
    }

    return @(
        $raw |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
            ForEach-Object { $_ | ConvertFrom-Json }
    )
}

function Get-ServiceStates {
    param([string[]]$ServiceNames)

    $rowsByService = @{}
    foreach ($row in Get-ComposeRows) {
        if ($null -ne $row.Service) {
            $rowsByService[[string]$row.Service] = $row
        }
    }

    return @(
        foreach ($service in $ServiceNames) {
            $row = $rowsByService[$service]
            if ($null -eq $row) {
                [pscustomobject]@{
                    service = $service
                    state = "missing"
                    health = $null
                    status = $null
                    exit_code = $null
                    running = $false
                }
                continue
            }

            [pscustomobject]@{
                service = $service
                state = [string]$row.State
                health = if ($null -ne $row.Health) { [string]$row.Health } else { $null }
                status = if ($null -ne $row.Status) { [string]$row.Status } else { $null }
                exit_code = $row.ExitCode
                running = ([string]$row.State -eq "running")
            }
        }
    )
}

function Write-Summary {
    param([object]$Summary)

    if (-not [string]::IsNullOrWhiteSpace($LogPath)) {
        $logDir = Split-Path -Parent $LogPath
        if (-not [string]::IsNullOrWhiteSpace($logDir)) {
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        }
        $Summary | ConvertTo-Json -Depth 6 -Compress | Add-Content -LiteralPath $LogPath -Encoding ASCII
    }

    if ($Json) {
        $Summary | ConvertTo-Json -Depth 6
        return
    }

    Write-Output ("[live-runtime-watchdog] ok={0} health_ok={1} mode={2} action={3} to_start={4}" -f `
        $Summary.ok, $Summary.health_ok, $Summary.mode, $Summary.action, ($Summary.services_to_start -join ","))
    foreach ($state in $Summary.after) {
        Write-Output ("[live-runtime-watchdog] {0}: state={1} health={2} status={3}" -f `
            $state.service, $state.state, $state.health, $state.status)
    }
}

$resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
Set-Location -LiteralPath $resolvedRoot

$envPath = Join-Path $resolvedRoot ".env"
$restartPolicy = if ($env:CHILI_LIVE_RUNTIME_RESTART_POLICY) {
    $env:CHILI_LIVE_RUNTIME_RESTART_POLICY
} else {
    Read-DotEnvValue -Path $envPath -Name "CHILI_LIVE_RUNTIME_RESTART_POLICY"
}

$watchdogDisabled = if ($env:CHILI_LIVE_RUNTIME_WATCHDOG_DISABLED) {
    $env:CHILI_LIVE_RUNTIME_WATCHDOG_DISABLED
} else {
    Read-DotEnvValue -Path $envPath -Name "CHILI_LIVE_RUNTIME_WATCHDOG_DISABLED"
}

$maintenanceLock = if ($env:CHILI_LIVE_RUNTIME_MAINTENANCE_LOCK) {
    $env:CHILI_LIVE_RUNTIME_MAINTENANCE_LOCK
} else {
    Join-Path $resolvedRoot ".chili-live-runtime-maintenance"
}

$disabledReason = $null
if (Test-Truthy -Value $watchdogDisabled) {
    $disabledReason = "CHILI_LIVE_RUNTIME_WATCHDOG_DISABLED"
} elseif (-not [string]::IsNullOrWhiteSpace($restartPolicy) -and
    (@("no", "none", "never", "disabled") -contains $restartPolicy.Trim().ToLowerInvariant())) {
    $disabledReason = "CHILI_LIVE_RUNTIME_RESTART_POLICY=$restartPolicy"
} elseif (Test-Path -LiteralPath $maintenanceLock) {
    $disabledReason = "maintenance_lock"
}

if ($disabledReason) {
    $summary = [ordered]@{
        ok = $true
        health_ok = $true
        mode = if ($DryRun) { "dry_run" } else { "apply" }
        action = "disabled"
        disabled_reason = $disabledReason
        timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
        services_checked = $Services
        services_to_start = @()
        services_unhealthy = @()
        services_starting = @()
        services_deferred = @()
        services_started = @()
        before = @()
        after = @()
    }
    Write-Summary -Summary $summary
    exit 0
}

$foundationLookup = @{}
foreach ($service in $FoundationServices) {
    $foundationLookup[$service] = $true
}

$before = @(Get-ServiceStates -ServiceNames $Services)
$toStart = @($before | Where-Object { -not $_.running } | ForEach-Object { $_.service })
$servicesStarted = @()
$servicesDeferred = @()
$action = "noop"

$foundationToStart = @(
    $before |
        Where-Object { -not $_.running -and $foundationLookup.ContainsKey($_.service) } |
        ForEach-Object { $_.service }
)

if ($foundationToStart.Count -gt 0) {
    if ($DryRun) {
        $action = "would_start_foundation"
    } else {
        $action = "started_foundation"
        $upArguments = @("compose", "up", "-d") + $foundationToStart
        $result = Invoke-NativeCapture -FilePath "docker" -Arguments $upArguments
        if ($result.exit_code -ne 0) {
            throw "docker compose up failed: $($result.output -join "`n")"
        }
        $servicesStarted += $foundationToStart

        if ($WaitSeconds -gt 0) {
            Start-Sleep -Seconds $WaitSeconds
        }
        $before = @(Get-ServiceStates -ServiceNames $Services)
    }
}

$foundationUnready = @(
    $before |
        Where-Object {
            $foundationLookup.ContainsKey($_.service) -and (
                -not $_.running -or (
                    -not [string]::IsNullOrWhiteSpace([string]$_.health) -and
                    [string]$_.health -ne "healthy"
                )
            )
        } |
        ForEach-Object { $_.service }
)

$dependentToStart = @(
    $before |
        Where-Object { -not $_.running -and -not $foundationLookup.ContainsKey($_.service) } |
        ForEach-Object { $_.service }
)

if ($dependentToStart.Count -gt 0) {
    if ($foundationUnready.Count -gt 0) {
        $servicesDeferred = $dependentToStart
        if ($action -eq "noop") {
            $action = if ($DryRun) { "would_defer_dependency_unready" } else { "deferred_dependency_unready" }
        } else {
            $action = "$action,deferred_dependency_unready"
        }
    } elseif ($DryRun) {
        if ($action -eq "noop") {
            $action = "would_start"
        }
    } else {
        if ($action -eq "noop") {
            $action = "started"
        } else {
            $action = "$action,started_dependents"
        }
        $upArguments = @("compose", "up", "-d") + $dependentToStart
        $result = Invoke-NativeCapture -FilePath "docker" -Arguments $upArguments
        if ($result.exit_code -ne 0) {
            throw "docker compose up failed: $($result.output -join "`n")"
        }
        $servicesStarted += $dependentToStart

        if ($WaitSeconds -gt 0) {
            Start-Sleep -Seconds $WaitSeconds
        }
    }
}

$after = if ($DryRun -or ($servicesStarted.Count -eq 0)) {
    $before
} else {
    @(Get-ServiceStates -ServiceNames $Services)
}

$notRunning = @($after | Where-Object { -not $_.running } | ForEach-Object { $_.service })
$unhealthy = @(
    $after |
        Where-Object {
            $_.running -and
            -not [string]::IsNullOrWhiteSpace([string]$_.health) -and
            [string]$_.health -eq "unhealthy"
        } |
        ForEach-Object { $_.service }
)
$starting = @(
    $after |
        Where-Object {
            $_.running -and
            -not [string]::IsNullOrWhiteSpace([string]$_.health) -and
            [string]$_.health -eq "starting"
        } |
        ForEach-Object { $_.service }
)
$healthNotReady = @(
    $after |
        Where-Object {
            $_.running -and
            -not [string]::IsNullOrWhiteSpace([string]$_.health) -and
            [string]$_.health -ne "healthy"
        } |
        ForEach-Object { $_.service }
)
$ok = ($notRunning.Count -eq 0)
$healthOk = ($healthNotReady.Count -eq 0)
$runtimeOk = $ok -and $healthOk

$summary = [ordered]@{
    ok = $ok
    health_ok = $healthOk
    runtime_ok = $runtimeOk
    mode = if ($DryRun) { "dry_run" } else { "apply" }
    action = $action
    timestamp_utc = (Get-Date).ToUniversalTime().ToString("o")
    services_checked = $Services
    services_to_start = $toStart
    services_started = $servicesStarted
    services_deferred = $servicesDeferred
    foundation_unready = $foundationUnready
    services_not_running = $notRunning
    services_unhealthy = $unhealthy
    services_starting = $starting
    services_health_not_ready = $healthNotReady
    before = $before
    after = $after
}
Write-Summary -Summary $summary

if ((-not $ok) -and (-not $DryRun)) {
    exit 1
}

exit 0
