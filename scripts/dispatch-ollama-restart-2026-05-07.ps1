# Restart Ollama + log container state.
# Triggered after diagnosis showed last viable LLM response was 16h ago
# (2026-05-07 02:29 UTC), with all subsequent autotrader LLM calls
# returning json-null or parse_failed with empty raw_preview.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-ollama-restart-2026-05-07-output.txt"
"# ollama restart $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"" | Add-Content $out
"## Pre-restart container state" | Add-Content $out
docker compose ps ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"## Last 30 ollama log lines (pre-restart)" | Add-Content $out
docker logs --tail=30 chili-ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"## Restart" | Add-Content $out
docker compose restart ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"## Wait 15s for boot + first health response" | Add-Content $out
Start-Sleep -Seconds 15

"" | Add-Content $out
"## Post-restart container state" | Add-Content $out
docker compose ps ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"## Last 20 ollama log lines (post-restart)" | Add-Content $out
docker logs --tail=20 chili-ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"## Smoke check: hit ollama API directly from chili-app container" | Add-Content $out
docker exec chili-app python -c "import urllib.request, json; r = urllib.request.urlopen('http://ollama:11434/api/tags', timeout=5); print('ok status=', r.status); print('body[:300]=', r.read()[:300])" 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
