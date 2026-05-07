# Re-probe LLM cascade after operator added OpenAI credits.
# The auth_failed skip-set is process-lifetime, so we MUST restart chili
# and autotrader-worker first to clear the suppressed providers from the
# prior cascade probe.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-llm-retest-2026-05-07-output.txt"
"# llm retest $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$auto = "chili-home-copilot-autotrader-worker-1"

"" | Add-Content $out
"## Step 1: force-recreate chili + autotrader-worker (clears _auth_failed_urls and reloads env)" | Add-Content $out
docker compose up -d --force-recreate chili autotrader-worker 2>&1 | Add-Content $out
Start-Sleep -Seconds 15

"" | Add-Content $out
"## Step 2: probe call_llm directly" | Add-Content $out
$probe = @'
from app.services.llm_caller import call_llm
from app.openai_client import is_configured
print("is_configured:", is_configured())
raw = call_llm(
    messages=[{"role": "user", "content": "Reply with the single word: OK"}],
    max_tokens=8,
    trace_id="retest-probe",
)
print("call_llm returned:", repr(raw)[:300])
'@
$probe | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 3: probe again 5s later (any cascade caching effect?)" | Add-Content $out
Start-Sleep -Seconds 5
$probe | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 4: probe a third time with a different prompt" | Add-Content $out
Start-Sleep -Seconds 3
$probe2 = @'
from app.services.llm_caller import call_llm
raw = call_llm(
    messages=[{"role": "user", "content": "Is 2+2 = 4? Answer yes or no."}],
    max_tokens=16,
    trace_id="retest-probe-2",
)
print("call_llm returned:", repr(raw)[:300])
'@
$probe2 | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
