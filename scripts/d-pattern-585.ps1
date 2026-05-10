$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-pattern-585-out.txt"
"# d-pattern-585 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

docker cp scripts/d-pattern-585.py chili-home-copilot-chili-1:/app/_p585.py 2>&1 | Add-Content $out
docker exec -w /app chili-home-copilot-chili-1 timeout 60 python /app/_p585.py 2>&1 | Add-Content $out
docker exec chili-home-copilot-chili-1 rm -f /app/_p585.py 2>&1 | Out-Null
"# end" | Add-Content $out
