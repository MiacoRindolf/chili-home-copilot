$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-rebuild-diagnose-out.txt"
"# d-env-rebuild-diagnose $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# container status" | Add-Content $out
docker ps -a --filter "name=chili-home-copilot" --format "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}" 2>&1 | Add-Content $out

"# step 1: chili-1 last 80 log lines (find import / parse error)" | Add-Content $out
docker logs --tail 80 chili-home-copilot-chili-1 2>&1 | Add-Content $out

"# step 2: autotrader-worker-1 last 50 log lines" | Add-Content $out
docker logs --tail 50 chili-home-copilot-autotrader-worker-1 2>&1 | Add-Content $out

"# end" | Add-Content $out
