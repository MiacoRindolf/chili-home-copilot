$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$taskName = "CHILI-phase5i-post-rename-soak-probe"
$script = Join-Path $repo "scripts\dispatch-phase5i-post-rename-soak-probe.ps1"

# Launch through the hidden wrapper (wscript GUI host) so the interactive
# task does not flash a console window on the operator's desktop each run.
$runHidden = Join-Path $repo "scripts\run-hidden.vbs"

$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$runHidden`" powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Days 14)
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
    -Description "Phase 5I post-rename soak probe for position-identity physical rename." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
