$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-autotrader-enable-fix-out.txt"
"# d-autotrader-enable-fix $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: run python repair (append clean lines)" | Add-Content $out
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}
& $pythonCmd scripts\d-autotrader-enable-fix.py 2>&1 | Add-Content $out

"# step 2: force-recreate workers" | Add-Content $out
docker compose up -d --force-recreate chili autotrader-worker scheduler-worker broker-sync-worker 2>&1 | Add-Content $out

Start-Sleep -Seconds 25

"# step 3: verify autotrader_enabled now True in 4 workers" | Add-Content $out
foreach ($c in @('chili-home-copilot-chili-1', 'chili-home-copilot-autotrader-worker-1', 'chili-home-copilot-scheduler-worker-1', 'chili-home-copilot-broker-sync-worker-1')) {
    "## $c" | Add-Content $out
    docker exec $c python -c @"
from app.config import settings
print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
print('chili_robinhood_spot_adapter_enabled:', settings.chili_robinhood_spot_adapter_enabled)
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
"@ 2>&1 | Add-Content $out
}

"# step 4: 30s observation -- look for autotrader cycle activity" | Add-Content $out
Start-Sleep -Seconds 35
docker logs --since 30s chili-home-copilot-autotrader-worker-1 2>&1 | Select-String -Pattern '(\[autotrader\]|selector:|cost_gate:|coinbase_cap:|tick scanned|skipped|placed|blocked)' | Select-Object -First 30 | ForEach-Object { $_.Line } | Add-Content $out

"# end" | Add-Content $out
