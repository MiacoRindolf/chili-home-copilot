$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-minimal-fix-out.txt"
"# d-env-minimal-fix $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# python forensic minimal-fix" | Add-Content $out
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}
if (-not $pythonCmd) {
    "ERROR: no python found" | Add-Content $out
    "# end (aborted)" | Add-Content $out
    exit 1
}

& $pythonCmd scripts\d-env-minimal-fix.py 2>&1 | Add-Content $out

"# force-recreate workers if any change happened" | Add-Content $out
docker compose up -d --force-recreate chili autotrader-worker scheduler-worker broker-sync-worker 2>&1 | Add-Content $out

Start-Sleep -Seconds 25

"# verify auth loads" | Add-Content $out
foreach ($c in @('chili-home-copilot-chili-1', 'chili-home-copilot-autotrader-worker-1', 'chili-home-copilot-scheduler-worker-1', 'chili-home-copilot-broker-sync-worker-1')) {
    "## $c" | Add-Content $out
    docker exec $c python -c @"
from app.config import settings
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
"@ 2>&1 | Add-Content $out
}

"# end" | Add-Content $out
