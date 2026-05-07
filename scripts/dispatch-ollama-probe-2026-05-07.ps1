# Direct probe of Ollama post-restart. Use correct compose-derived container names.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-ollama-probe-2026-05-07-output.txt"
"# ollama probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"" | Add-Content $out
"## Container names" | Add-Content $out
docker compose ps --format 'table {{.Name}}\t{{.Service}}\t{{.Status}}' 2>&1 | Add-Content $out

"" | Add-Content $out
"## Ollama API tags (loaded models)" | Add-Content $out
docker compose exec -T chili python -c "import urllib.request, json; r = urllib.request.urlopen('http://ollama:11434/api/tags', timeout=5); print(json.dumps(json.loads(r.read()), indent=2)[:600])" 2>&1 | Add-Content $out

"" | Add-Content $out
"## Quick LLM generate test (small prompt)" | Add-Content $out
$payload = '{"model":"qwen2.5:3b","prompt":"Reply with just the word: OK","stream":false}'
docker compose exec -T chili python -c "import urllib.request, json; req = urllib.request.Request('http://ollama:11434/api/generate', data=b'$payload', headers={'Content-Type':'application/json'}); r = urllib.request.urlopen(req, timeout=15); body = json.loads(r.read()); print('response:', body.get('response', body)[:200]); print('done:', body.get('done'))" 2>&1 | Add-Content $out

"" | Add-Content $out
"## Last 30 ollama log lines" | Add-Content $out
docker compose logs --tail=30 ollama 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
