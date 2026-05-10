$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-rebuild-out.txt"
"# d-env-rebuild $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: pre-flight -- verify Coinbase auth currently loads" | Add-Content $out
$pre = docker exec chili-home-copilot-chili-1 python -c @"
from app.config import settings
ok = bool(settings.coinbase_api_key) and bool(settings.coinbase_api_secret)
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
print('PREFLIGHT_OK:', ok)
"@ 2>&1
$pre | Add-Content $out

if ((($pre -join "`n") -notmatch 'PREFLIGHT_OK:\s*True')) {
    "# ABORT: pre-state not healthy (auth not loading); refusing to rebuild" | Add-Content $out
    "# end (aborted)" | Add-Content $out
    exit 1
}

"# step 2: run python rebuild (ASCII bytes, lossless validation)" | Add-Content $out
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}
if (-not $pythonCmd) {
    "ERROR: no python found" | Add-Content $out
    exit 2
}

$rebuildResult = & $pythonCmd scripts\d-env-rebuild.py 2>&1
$rebuildResult | Add-Content $out

# Check whether rebuild succeeded (look for "# done")
if ((($rebuildResult -join "`n") -notmatch '#\s*done')) {
    "# ABORT: rebuild did not complete; .env unchanged or write failed" | Add-Content $out
    "# end (aborted)" | Add-Content $out
    exit 3
}

"# step 3: force-recreate workers" | Add-Content $out
docker compose up -d --force-recreate chili autotrader-worker scheduler-worker broker-sync-worker 2>&1 | Add-Content $out

Start-Sleep -Seconds 25

"# step 4: post-rebuild verification (4 workers)" | Add-Content $out
foreach ($c in @('chili-home-copilot-chili-1', 'chili-home-copilot-autotrader-worker-1', 'chili-home-copilot-scheduler-worker-1', 'chili-home-copilot-broker-sync-worker-1')) {
    "## $c" | Add-Content $out
    docker exec $c python -c @"
from app.config import settings
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
print('chili_robinhood_spot_adapter_enabled:', settings.chili_robinhood_spot_adapter_enabled)
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
print('chili_autotrader_kill_switch:', settings.chili_autotrader_kill_switch)
print('chili_coinbase_max_notional_usd:', settings.chili_coinbase_max_notional_usd)
"@ 2>&1 | Add-Content $out
}

"# step 5: spot-check a sample of vars that were previously broken" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
import os
from app.config import settings
# These came from .env originally and were corrupted by the giant-line collapse
checks = [
    ('database_url', getattr(settings, 'database_url', None)),
    ('llm_api_key', os.environ.get('LLM_API_KEY', 'MISSING')[:8] + '...' if os.environ.get('LLM_API_KEY') else 'MISSING'),
    ('llm_model', os.environ.get('LLM_MODEL', 'MISSING')),
    ('llm_base_url', os.environ.get('LLM_BASE_URL', 'MISSING')),
    ('massive_api_key_prefix', os.environ.get('MASSIVE_API_KEY', 'MISSING')[:8] + '...' if os.environ.get('MASSIVE_API_KEY') else 'MISSING'),
    ('robinhood_username', os.environ.get('ROBINHOOD_USERNAME', 'MISSING')),
    ('telegram_bot_token_prefix', os.environ.get('TELEGRAM_BOT_TOKEN', 'MISSING')[:12] + '...' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'MISSING'),
    ('email_user', os.environ.get('EMAIL_USER', 'MISSING')),
    ('smtp_host', os.environ.get('SMTP_HOST', 'MISSING')),
    ('smtp_port', os.environ.get('SMTP_PORT', 'MISSING')),
    ('zerox_api_key', os.environ.get('ZEROX_API_KEY', 'MISSING')[:12] + '...' if os.environ.get('ZEROX_API_KEY') else 'MISSING'),
    ('use_polygon', os.environ.get('USE_POLYGON', 'MISSING')),
]
for name, val in checks:
    print(f'  {name}: {val}')
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
