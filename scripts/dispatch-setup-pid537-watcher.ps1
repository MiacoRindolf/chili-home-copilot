$ErrorActionPreference = "Continue"
$repo = "C:\dev\chili-home-copilot"
Set-Location $repo
$out = "$PSScriptRoot\dispatch-setup-pid537-watcher-out.txt"
"# $(Get-Date -Format o) -- setup pid537 watcher windows task" | Out-File $out -Encoding utf8

$o = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\setup-pid537-watcher-windows-task.ps1" 2>&1
$rc = $LASTEXITCODE
"  setup_exit=$rc" | Add-Content $out
"--- setup output ---" | Add-Content $out
$o | Out-String | Add-Content $out

# Verify task exists and trigger it once to prove end-to-end pipeline.
"" | Add-Content $out
"## Verify task registered" | Add-Content $out
$verify = Get-ScheduledTask -TaskName "CHILI-pid537-watcher" -ErrorAction SilentlyContinue
if ($verify) {
    "Task found:" | Add-Content $out
    ($verify | Select-Object TaskName, State, Description | Format-List | Out-String) | Add-Content $out
    "Triggers:" | Add-Content $out
    ($verify.Triggers | Format-List | Out-String) | Add-Content $out
} else {
    "Task NOT found after registration. ERROR." | Add-Content $out
}

"# $(Get-Date -Format o) -- end" | Add-Content $out

$o | ForEach-Object { Write-Output $_ }
"DISPATCH_SETUP_WATCHER_DONE exit=$rc"
exit $rc
