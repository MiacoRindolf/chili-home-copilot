$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-restore-and-rebuild-out.txt"
"# d-env-restore-and-rebuild $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 0: restore .env from .env.prerebuild backup (UNDO bad rebuild)" | Add-Content $out
if (-not (Test-Path ".env.prerebuild")) {
    "  ABORT: .env.prerebuild backup not found" | Add-Content $out
    exit 1
}
$bkBytes = [System.IO.File]::ReadAllBytes(".env.prerebuild")
[System.IO.File]::WriteAllBytes(".env", $bkBytes)
"  .env restored from .env.prerebuild ($($bkBytes.Length) bytes)" | Add-Content $out

# Remove .env.prerebuild so the next rebuild can write a fresh backup
Remove-Item ".env.prerebuild" -ErrorAction SilentlyContinue

"# step 1: pre-flight after restore" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from app.config import settings
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
print('PREFLIGHT_OK:', bool(settings.coinbase_api_key) and bool(settings.coinbase_api_secret))
"@ 2>&1 | Add-Content $out

"# step 2: run python rebuild (with name-boundary fix)" | Add-Content $out
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}
$rebuildResult = & $pythonCmd scripts\d-env-rebuild.py 2>&1
$rebuildResult | Add-Content $out

if ((($rebuildResult -join "`n") -notmatch '#\s*done')) {
    "# ABORT: rebuild did not complete; .env unchanged" | Add-Content $out
    "# end (aborted)" | Add-Content $out
    exit 2
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

"# step 5: spot-check vars that were previously broken" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
import os
checks = [
    'LLM_API_KEY', 'LLM_MODEL', 'LLM_BASE_URL',
    'PREMIUM_API_KEY', 'PREMIUM_MODEL', 'PREMIUM_BASE_URL',
    'ZEROX_API_KEY', 'ROBINHOOD_USERNAME', 'ROBINHOOD_PASSWORD',
    'PAID_OPENAI_API_KEY', 'PAID_OPENAI_MODEL', 'PAID_OPENAI_BASE_URL',
    'SMS_PHONE', 'SMS_CARRIER', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
    'EMAIL_USER', 'EMAIL_PASSWORD', 'SMTP_HOST', 'SMTP_PORT',
    'MASSIVE_API_KEY', 'MASSIVE_USE_WEBSOCKET', 'POLYGON_API_KEY',
    'POLYGON_BASE_URL', 'USE_POLYGON', 'DATABASE_URL', 'TEST_DATABASE_URL',
    'CHILI_DISPATCH_GITHUB_TOKEN',
    'CHILI_AUTOTRADER_USER_ID', 'CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS',
    'PATTERN_IMMINENT_MIN_READINESS',
]
for k in checks:
    v = os.environ.get(k, 'MISSING')
    if v == 'MISSING':
        print(f'  {k}: MISSING')
    elif len(v) > 30:
        # Mask long values (likely secrets)
        print(f'  {k}: <set, len={len(v)}, prefix={v[:8]}...>')
    else:
        print(f'  {k}: {v}')
"@ 2>&1 | Add-Content $out

"# step 6: line count + sanity (final .env structure)" | Add-Content $out
$final = [System.IO.File]::ReadAllBytes(".env")
$lineCount = ([System.Text.Encoding]::ASCII.GetString($final).Split("`n")).Count
"  final .env: $($final.Length) bytes, $lineCount lines" | Add-Content $out
if ($final[0] -eq 0xEF) { "  WARN: BOM present!" | Add-Content $out } else { "  no BOM (good)" | Add-Content $out }

"# end" | Add-Content $out
