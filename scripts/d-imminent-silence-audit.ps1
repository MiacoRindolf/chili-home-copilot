$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-imminent-silence-audit-out.txt"
"# d-imminent-silence-audit $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# copy script into chili container" | Add-Content $out
docker cp scripts/d-imminent-silence-audit.py chili-home-copilot-chili-1:/app/d-imminent-silence-audit.py 2>&1 | Add-Content $out

"# run audit (90s timeout for large queries)" | Add-Content $out
docker exec -w /app chili-home-copilot-chili-1 timeout 90 python /app/d-imminent-silence-audit.py 2>&1 | Add-Content $out

"# cleanup" | Add-Content $out
docker exec chili-home-copilot-chili-1 rm -f /app/d-imminent-silence-audit.py 2>&1 | Out-Null

"# end" | Add-Content $out
