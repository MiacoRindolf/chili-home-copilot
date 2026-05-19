$ErrorActionPreference = "Continue"
$repo = "C:\dev\chili-home-copilot"
Set-Location $repo
$out = "$PSScriptRoot\dispatch-maker-only-soak-start-out.txt"
"# $(Get-Date -Format o) -- maker-only paper-soak start" | Out-File $out -Encoding utf8

# ---- STEP 1: Capture pre-flip baseline ----
"" | Add-Content $out
"## STEP 1: pre-flip TCA baseline (Coinbase + RH crypto, last 30d)" | Add-Content $out
$baselineSQL = @"
SELECT
  broker_source,
  COUNT(*) AS n_trades,
  ROUND(AVG(tca_entry_slippage_bps)::numeric, 2) AS avg_entry_bps,
  ROUND(STDDEV(tca_entry_slippage_bps)::numeric, 2) AS sd_bps,
  ROUND(MIN(tca_entry_slippage_bps)::numeric, 2) AS min_bps,
  ROUND(MAX(tca_entry_slippage_bps)::numeric, 2) AS max_bps
FROM trading_trades
WHERE tca_entry_slippage_bps IS NOT NULL
  AND entry_date > NOW() - INTERVAL '30 days'
  AND (broker_source = 'coinbase' OR ticker LIKE '%-USD')
GROUP BY broker_source
ORDER BY broker_source;
"@
(docker compose exec -T postgres psql -U chili -d chili -c $baselineSQL 2>&1 | Out-String) | Add-Content $out

"" | Add-Content $out
"## STEP 1b: Coinbase-only baseline (the target cohort)" | Add-Content $out
$cbSQL = "SELECT COUNT(*) AS n, ROUND(AVG(tca_entry_slippage_bps)::numeric, 2) AS avg_bps, ROUND(STDDEV(tca_entry_slippage_bps)::numeric, 2) AS sd_bps FROM trading_trades WHERE broker_source='coinbase' AND tca_entry_slippage_bps IS NOT NULL AND entry_date > NOW() - INTERVAL '30 days'"
(docker compose exec -T postgres psql -U chili -d chili -c $cbSQL 2>&1 | Out-String) | Add-Content $out

# ---- STEP 2: Flip flag in .env (ASCII WriteAllBytes per memory) ----
"" | Add-Content $out
"## STEP 2: flip CHILI_COINBASE_MAKER_ONLY_ENABLED=true in .env" | Add-Content $out

$envPath = "$repo\.env"
$flagName = "CHILI_COINBASE_MAKER_ONLY_ENABLED"
$flagLine = "$flagName=true"

if (-not (Test-Path $envPath)) {
    "  ERROR: .env not found at $envPath" | Add-Content $out
    exit 1
}
$bytes = [System.IO.File]::ReadAllBytes($envPath)
$content = [System.Text.Encoding]::ASCII.GetString($bytes)
$lines = $content -split "`r?`n"
$found = $false
$newLines = @()
foreach ($ln in $lines) {
    if ($ln -match "^\s*$flagName\s*=") {
        "  REPLACING existing line: $ln" | Add-Content $out
        $newLines += $flagLine
        $found = $true
    } else {
        $newLines += $ln
    }
}
if (-not $found) {
    "  APPENDING new flag line" | Add-Content $out
    if ($newLines.Count -eq 0 -or $newLines[-1] -ne "") { $newLines += "" }
    $newLines += $flagLine
}
$newContent = ($newLines -join "`n")
$newBytes = [System.Text.Encoding]::ASCII.GetBytes($newContent)
[System.IO.File]::WriteAllBytes($envPath, $newBytes)

# Verify
$verifyBytes = [System.IO.File]::ReadAllBytes($envPath)
$verifyText = [System.Text.Encoding]::ASCII.GetString($verifyBytes)
$verifyLine = ($verifyText -split "`r?`n") | Where-Object { $_ -match "$flagName" }
"  POST-WRITE verify: $verifyLine" | Add-Content $out

# ---- STEP 3: Restart autotrader-worker (lowest blast radius) ----
"" | Add-Content $out
"## STEP 3: docker compose up -d --force-recreate autotrader-worker" | Add-Content $out
(docker compose up -d --force-recreate autotrader-worker 2>&1 | Out-String) | Add-Content $out

# Wait for it to start up
Start-Sleep -Seconds 20

# ---- STEP 4: Watch initial logs ----
"" | Add-Content $out
"## STEP 4: autotrader-worker logs (last 100 lines)" | Add-Content $out
(docker compose logs --tail 100 autotrader-worker 2>&1 | Out-String) | Add-Content $out

"" | Add-Content $out
"## STEP 4b: filtered maker-only / fallback log lines" | Add-Content $out
$logs = docker compose logs --tail 300 autotrader-worker 2>&1
$filtered = $logs | Select-String -Pattern "maker-only|place_limit_order_gtc|falling back to market|_chili_maker_only"
if ($filtered) {
    $filtered | ForEach-Object { $_.Line } | Out-String | Add-Content $out
} else {
    "  no maker-only log lines yet (waiting for first Coinbase autotrader attempt)" | Add-Content $out
}

# ---- STEP 5: confirm flag is live in container env ----
"" | Add-Content $out
"## STEP 5: confirm flag live inside autotrader-worker container" | Add-Content $out
$envCheckCmd = "docker compose exec -T autotrader-worker sh -c `"env | grep MAKER_ONLY`""
(Invoke-Expression $envCheckCmd 2>&1 | Out-String) | Add-Content $out

"# $(Get-Date -Format o) -- end" | Add-Content $out
"DISPATCH_MAKER_ONLY_SOAK_START_DONE"
Get-Content $out -Tail 50
