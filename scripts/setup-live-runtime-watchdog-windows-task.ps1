<#
.SYNOPSIS
  Installs the CHILI live-runtime watchdog as a Windows scheduled task.

.DESCRIPTION
  The task runs scripts/watch-live-runtime.ps1 at a short interval so explicit
  docker stop/kill events do not strand Postgres/Ollama foundations, broker
  truth, autotrader, fast scan, market snapshots, or the web/API down.

  The watchdog itself has maintenance escape hatches documented in
  scripts/watch-live-runtime.ps1.
#>
[CmdletBinding()]
param(
    [int]$IntervalMinutes = 1,
    [string]$TaskName = "CHILI-live-runtime-watchdog"
)

$ErrorActionPreference = "Stop"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$script = Join-Path $repo "scripts\watch-live-runtime.ps1"
$log = Join-Path $repo "logs\live-runtime-watchdog.jsonl"

if (-not (Test-Path -LiteralPath $script)) {
    throw "Watchdog script not found: $script"
}

$argument = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$script`"",
    "-Root", "`"$repo`"",
    "-LogPath", "`"$log`"",
    "-Json"
) -join " "

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $argument

$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "[setup-live-runtime-watchdog] removing existing task '$TaskName'"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "CHILI live trading runtime watchdog. Starts stopped compose services: postgres, ollama, chili, broker-sync-worker, autotrader-worker, fast-scan-worker, market-snapshot-worker. Does not remove data or start heavy/offline lanes." `
    -Force | Out-Null

Write-Output "[setup-live-runtime-watchdog] task '$TaskName' registered, every $IntervalMinutes minute(s)"
Write-Output "[setup-live-runtime-watchdog] verify: Get-ScheduledTask -TaskName $TaskName"
Write-Output "[setup-live-runtime-watchdog] trigger manually: Start-ScheduledTask -TaskName $TaskName"
Write-Output "[setup-live-runtime-watchdog] log: $log"
Write-Output "[setup-live-runtime-watchdog] pause maintenance: New-Item -ItemType File .chili-live-runtime-maintenance"
Write-Output "[setup-live-runtime-watchdog] remove: Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"
