$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-env-restore-out.txt"
"# d-env-restore $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# run python forensic restore (host python; never echoes values)" | Add-Content $out

# Try several common Python invocations; first one that exists wins
$pythonCmd = $null
foreach ($cand in @('python', 'py', 'python3')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $pythonCmd = $cand; break }
}

if (-not $pythonCmd) {
    "ERROR: no python interpreter found on host PATH" | Add-Content $out
    "# end (aborted)" | Add-Content $out
    exit 1
}

"  using: $pythonCmd" | Add-Content $out
& $pythonCmd scripts\d-env-restore.py 2>&1 | Add-Content $out

"# verify in chili container after restore" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from app.config import settings
print('coinbase_api_key_set:', bool(settings.coinbase_api_key))
print('coinbase_api_secret_set:', bool(settings.coinbase_api_secret))
print('chili_coinbase_autotrader_live:', settings.chili_coinbase_autotrader_live)
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
