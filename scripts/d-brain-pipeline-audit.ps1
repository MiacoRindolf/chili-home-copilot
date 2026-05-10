$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-brain-pipeline-audit-out.txt"
"# d-brain-pipeline-audit $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# copy script into chili container" | Add-Content $out
docker cp scripts/d-brain-pipeline-audit.py chili-home-copilot-chili-1:/app/_brain_pipeline_audit.py 2>&1 | Add-Content $out

"# run audit (timeout 90s)" | Add-Content $out
docker exec -w /app chili-home-copilot-chili-1 timeout 90 python /app/_brain_pipeline_audit.py 2>&1 | Add-Content $out

"# cleanup" | Add-Content $out
docker exec chili-home-copilot-chili-1 rm -f /app/_brain_pipeline_audit.py 2>&1 | Out-Null

"# end" | Add-Content $out
