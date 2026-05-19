$ErrorActionPreference = "Continue"
$out = "$PSScriptRoot\dispatch-trigger-pid537-watcher-out.txt"
"# $(Get-Date -Format o) -- manual trigger of CHILI-pid537-watcher" | Out-File $out -Encoding utf8

try {
    Start-ScheduledTask -TaskName 'CHILI-pid537-watcher'
    "Start-ScheduledTask invoked OK" | Add-Content $out
} catch {
    "Start-ScheduledTask FAILED: $_" | Add-Content $out
    exit 1
}

Start-Sleep -Seconds 3
"--- _claude_pending.txt after trigger ---" | Add-Content $out
Get-Content "$PSScriptRoot\_claude_pending.txt" -ErrorAction SilentlyContinue | Add-Content $out

"# $(Get-Date -Format o) -- end" | Add-Content $out
Write-Output "TRIGGER_DONE"
