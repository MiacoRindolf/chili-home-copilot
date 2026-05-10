$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-autotrader-enabled-probe-out.txt"
"# d-autotrader-enabled-probe $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: current value of chili_autotrader_enabled in 4 workers" | Add-Content $out
foreach ($c in @('chili-home-copilot-chili-1', 'chili-home-copilot-autotrader-worker-1', 'chili-home-copilot-scheduler-worker-1', 'chili-home-copilot-broker-sync-worker-1')) {
    "## $c" | Add-Content $out
    docker exec $c python -c @"
from app.config import settings
print('chili_autotrader_enabled:', settings.chili_autotrader_enabled)
print('chili_autotrader_live_enabled:', getattr(settings, 'chili_autotrader_live_enabled', 'MISSING'))
print('chili_autotrader_kill_switch:', settings.chili_autotrader_kill_switch)
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
"@ 2>&1 | Add-Content $out
}

"# step 2: is CHILI_AUTOTRADER_ENABLED in .env?" | Add-Content $out
$bytes = [System.IO.File]::ReadAllBytes(".env")
$content = [System.Text.Encoding]::UTF8.GetString($bytes)
if ($content -match 'CHILI_AUTOTRADER_ENABLED\s*=\s*(\S+)') {
    "  found in .env: CHILI_AUTOTRADER_ENABLED=$($Matches[1])" | Add-Content $out
} else {
    "  NOT in .env (value=False default)" | Add-Content $out
}

"# step 3: docker-compose env (might set it via environment:)" | Add-Content $out
docker exec chili-home-copilot-autotrader-worker-1 bash -c "env | grep -i autotrader | sort" 2>&1 | Add-Content $out

"# end" | Add-Content $out
