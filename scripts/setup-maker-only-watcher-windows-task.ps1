# Idempotent installer for the weekly maker-only TCA re-probe via
# Windows Task Scheduler. Mirrors the pid537 watcher pattern.
#
# Fires Sundays at 18:00 local time; writes a dispatch line to
# scripts/_claude_pending.txt; the dev daemon picks it up + runs the
# TCA probe. Output goes to scripts/dispatch-maker-only-tca-probe-out.txt.

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = "CHILI-maker-only-tca-probe"
$pendingFile = Join-Path $repo 'scripts\_claude_pending.txt'

$dispatchLine = "TIMEOUT=60s .\scripts\dispatch-maker-only-tca-probe.ps1"
$argList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$repo'; '$dispatchLine' | Out-File -FilePath '$pendingFile' -Encoding ASCII -Force"
)

# Launch through the hidden wrapper (wscript GUI host) so the interactive
# task does not flash a console window on the operator's desktop each run.
$runHidden = Join-Path $repo 'scripts\run-hidden.vbs'

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument ("`"$runHidden`" powershell.exe " + ($argList -join " "))

# Weekly: Sunday at 18:00 local
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "18:00"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "[setup-maker-tca-watcher] removing existing task '$taskName'"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "f-coinbase-maker-only-paper-soak watcher (created 2026-05-19). Fires weekly Sunday 18:00 local; writes a dispatch line to scripts/_claude_pending.txt so the dev daemon runs scripts/dispatch-maker-only-tca-probe.ps1. Output goes to scripts/dispatch-maker-only-tca-probe-out.txt. Operator should read the VERDICT line to decide promote vs rollback." `
    -Force

Write-Output "[setup-maker-tca-watcher] task '$taskName' registered, weekly Sun 18:00 local"
Write-Output "[setup-maker-tca-watcher] verify: Get-ScheduledTask -TaskName $taskName"
Write-Output "[setup-maker-tca-watcher] trigger manually: Start-ScheduledTask -TaskName $taskName"
Write-Output "[setup-maker-tca-watcher] remove: Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
