# LLM cascade diagnosis: env vars, recent auth failures, and post-restart probe.
# Triggered after Ollama restart was confirmed irrelevant (call_llm goes through
# openai_client.chat, not Ollama). Goal: identify which provider is failing and
# whether a chili restart clears the _auth_failed_urls skip set.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$out = "scripts\dispatch-llm-cascade-diag-2026-05-07-output.txt"
"# llm cascade diag $(Get-Date -Format o)" | Out-File $out -Encoding utf8

$chili = "chili-home-copilot-chili-1"
$auto  = "chili-home-copilot-autotrader-worker-1"

"" | Add-Content $out
"## Step 1: env vars in chili (API keys + LLM config)" | Add-Content $out
docker exec $chili sh -c 'env | grep -E "^(OPENAI_API_KEY|LLM_API_KEY|GROQ_API_KEY|GEMINI_API_KEY|OPENAI_BASE_URL|LLM_BASE_URL|OLLAMA_HOST|LLM_MODEL|OPENAI_MODEL)" | sed -E "s/(KEY=)([A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]*/\1\2(redacted)/g"' 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 2: same env vars in autotrader-worker" | Add-Content $out
docker exec $auto sh -c 'env | grep -E "^(OPENAI_API_KEY|LLM_API_KEY|GROQ_API_KEY|GEMINI_API_KEY|OPENAI_BASE_URL|LLM_BASE_URL|OLLAMA_HOST|LLM_MODEL|OPENAI_MODEL)" | sed -E "s/(KEY=)([A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]*/\1\2(redacted)/g"' 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 3: recent chili logs for auth/401/llm errors (last 200, grep)" | Add-Content $out
docker logs --tail=400 $chili 2>&1 | Select-String -Pattern '401|auth_failed|api_key|llm_caller|openai|groq|provider|gateway' -CaseSensitive:$false | Select-Object -Last 30 | Add-Content $out

"" | Add-Content $out
"## Step 4: same on autotrader-worker" | Add-Content $out
docker logs --tail=400 $auto 2>&1 | Select-String -Pattern '401|auth_failed|api_key|llm_caller|openai|groq|provider|llm_not_viable|parse_failed' -CaseSensitive:$false | Select-Object -Last 30 | Add-Content $out

"" | Add-Content $out
"## Step 5: test call_llm directly from autotrader-worker (probes the live cascade)" | Add-Content $out
$probe = @'
import sys
try:
    from app.services.llm_caller import call_llm
    from app.openai_client import is_configured
    print("is_configured:", is_configured())
    raw = call_llm(
        messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        max_tokens=8,
        trace_id="cascade-probe",
    )
    print("call_llm returned:", repr(raw)[:300])
except Exception as e:
    import traceback
    print("EXCEPTION:", type(e).__name__, str(e)[:300])
    traceback.print_exc(limit=5)
'@
$probe | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 6: restart chili-app (clears _auth_failed_urls skip set)" | Add-Content $out
docker compose restart chili 2>&1 | Add-Content $out
Start-Sleep -Seconds 10

"" | Add-Content $out
"## Step 7: post-restart probe again" | Add-Content $out
$probe | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"## Step 8: also restart autotrader-worker so it loads fresh skip-set state" | Add-Content $out
docker compose restart autotrader-worker 2>&1 | Add-Content $out
Start-Sleep -Seconds 10

"" | Add-Content $out
"## Step 9: post-autotrader-restart probe" | Add-Content $out
$probe | docker exec -i $auto python 2>&1 | Add-Content $out

"" | Add-Content $out
"---" | Add-Content $out
"# end" | Add-Content $out
