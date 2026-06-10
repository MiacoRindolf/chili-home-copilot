$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$taskName = "CHILI-phase5e-reporting-soak-probe"
$script = Join-Path $repo "scripts\dispatch-phase5e-reporting-soak-probe.ps1"

# Launch through the hidden wrapper (wscript GUI host) so the interactive
# task does not flash a console window on the operator's desktop each run.
$runHidden = Join-Path $repo "scripts\run-hidden.vbs"

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$runHidden`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At 6:20pm
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Daily Phase 5E reporting-reader soak probe for position-identity rename gate." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
