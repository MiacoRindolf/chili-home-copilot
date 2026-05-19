$ErrorActionPreference = "Continue"
$repo = "C:\dev\chili-home-copilot"
Set-Location $repo
$out = "$PSScriptRoot\dispatch-setup-maker-only-watcher-out.txt"
"# $(Get-Date -Format o) -- setup maker-only TCA watcher" | Out-File $out -Encoding utf8

$o = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\setup-maker-only-watcher-windows-task.ps1" 2>&1
$rc = $LASTEXITCODE
"  exit_code=$rc" | Add-Content $out
"--- setup output ---" | Add-Content $out
$o | Out-String | Add-Content $out

"" | Add-Content $out
"## Verify task registered" | Add-Content $out
$verify = Get-ScheduledTask -TaskName "CHILI-maker-only-tca-probe" -ErrorAction SilentlyContinue
if ($verify) {
    "Task found:" | Add-Content $out
    ($verify | Select-Object TaskName, State, Description | Format-List | Out-String) | Add-Content $out
    "Triggers:" | Add-Content $out
    ($verify.Triggers | Format-List | Out-String) | Add-Content $out
} else {
    "Task NOT found after registration. ERROR." | Add-Content $out
}

# Also run the probe once now to capture the T+0 reading
"" | Add-Content $out
"## Initial probe (T+0)" | Add-Content $out
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili" }
$probeOut = & conda run -n chili-env python "$repo\scripts\d-maker-only-tca-probe.py" 2>&1
$probeOut | Out-String | Add-Content $out

"# $(Get-Date -Format o) -- end" | Add-Content $out
"DISPATCH_SETUP_MAKER_ONLY_WATCHER_DONE"
Get-Content $out -Tail 35
