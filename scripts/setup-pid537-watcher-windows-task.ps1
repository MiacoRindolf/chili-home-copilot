# Idempotent installer for the daily pid 537 watcher via Windows Task Scheduler.
#
# Creates a scheduled task 'CHILI-pid537-watcher' that fires daily at 18:00 local
# time and writes a single line to scripts/_claude_pending.txt — the dev daemon
# (which polls every 2s) picks it up and runs the watcher dispatch.
#
# Re-running the script is safe: existing task is unregistered first.

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskName = "CHILI-pid537-watcher"
$pendingFile = Join-Path $repo 'scripts\_claude_pending.txt'

# The action: write a single line to _claude_pending.txt. Use Out-File with
# UTF8NoBOM-equivalent so the daemon (which expects plain ASCII) doesn't choke
# on a BOM. PowerShell 5.1's Out-File adds BOM by default; force ASCII to
# avoid that.
$dispatchLine = "TIMEOUT=60s .\scripts\dispatch-pid537-watcher.ps1"
$argList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Set-Location '$repo'; '$dispatchLine' | Out-File -FilePath '$pendingFile' -Encoding ASCII -Force"
)

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($argList -join " ")

# Daily at 18:00 local time
$trigger = New-ScheduledTaskTrigger -Daily -At "18:00"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10)

# Unregister any existing task with the same name (idempotent re-run).
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "[setup-watcher] removing existing task '$taskName'"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "f-pattern-537-evaluation watcher (created 2026-05-18). Fires daily at 18:00 local time and writes a dispatch line to scripts/_claude_pending.txt so the dev daemon runs scripts/dispatch-pid537-watcher.ps1. Output goes to scripts/dispatch-pid537-watcher-out.txt. Operator should run 'claude' periodically to read the verdict and act on COMPLETE/ALERT/REGRESSION states." `
    -Force

Write-Output "[setup-watcher] task '$taskName' registered, daily at 18:00 local"
Write-Output "[setup-watcher] verify: Get-ScheduledTask -TaskName $taskName"
Write-Output "[setup-watcher] trigger manually: Start-ScheduledTask -TaskName $taskName"
Write-Output "[setup-watcher] remove: Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
