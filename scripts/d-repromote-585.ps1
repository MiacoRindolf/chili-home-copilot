$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-repromote-585-out.txt"
"# d-repromote-585 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

docker cp scripts/d-repromote-585.py chili-home-copilot-chili-1:/app/_repromote_585.py 2>&1 | Add-Content $out
docker exec -w /app chili-home-copilot-chili-1 timeout 30 python /app/_repromote_585.py 2>&1 | Add-Content $out
docker exec chili-home-copilot-chili-1 rm -f /app/_repromote_585.py 2>&1 | Out-Null

"# end" | Add-Content $out
