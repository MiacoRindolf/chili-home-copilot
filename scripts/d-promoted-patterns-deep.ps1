$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-promoted-patterns-deep-out.txt"
"# d-promoted-patterns-deep $(Get-Date -Format o)" | Out-File $out -Encoding utf8

docker cp scripts/d-promoted-patterns-deep.py chili-home-copilot-chili-1:/app/_promoted_deep.py 2>&1 | Add-Content $out
docker exec -w /app chili-home-copilot-chili-1 timeout 60 python /app/_promoted_deep.py 2>&1 | Add-Content $out
docker exec chili-home-copilot-chili-1 rm -f /app/_promoted_deep.py 2>&1 | Out-Null

"# end" | Add-Content $out
