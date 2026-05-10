# dispatch-coinbase-orphan-adopt.ps1
#
# One-shot adoption pass for Coinbase orphan stops
# (f-coinbase-orphan-stop-adoption, 2026-05-10).
#
# Background: after f-coinbase-post-place-verify-routing-fix (commit
# c8a3ff3) sealed the Robinhood-routing bug, four trades (AERGO, 1INCH,
# ACX, RARE) were left with working SELL stop-limit orders at Coinbase
# but DB-naked broker_stop_order_id. This script pulls open Coinbase
# stops, matches each to a single naked trading_bracket_intents row by
# (ticker, qty), and persists broker_stop_order_id + transitions
# intent_state to reconciled.
#
# Default: DRY-RUN (prints what WOULD be adopted, no DB writes).
# Pass -Apply to commit the adoptions.
#
# Output: scripts/dispatch-coinbase-orphan-adopt-output.txt + stdout.

[CmdletBinding()]
param(
    [switch]$Apply
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "scripts/dispatch-coinbase-orphan-adopt-output.txt"
$start = Get-Date

$mode = if ($Apply) { "APPLY" } else { "DRY-RUN" }
"# coinbase orphan-stop adoption pass ($mode) $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# ── Pre-adoption state snapshot ──────────────────────────────────────
"" | Add-Content $out
"---DB: open Coinbase trades + bracket_intent state (BEFORE)---" | Add-Content $out
$env:PGPASSWORD = "chili"
$sqlBefore = @"
SELECT t.id AS trade_id, t.ticker, bi.id AS intent_id,
       bi.intent_state, bi.broker_stop_order_id
  FROM trading_trades t
  JOIN trading_bracket_intents bi ON bi.trade_id = t.id
 WHERE t.status='open' AND t.broker_source='coinbase'
 ORDER BY t.id;
"@
psql -h localhost -p 5433 -U chili -d chili -c "$sqlBefore" 2>&1 | Out-String | Add-Content $out

# ── Run adoption pass ────────────────────────────────────────────────
"" | Add-Content $out
"---adoption pass ($mode)---" | Add-Content $out

$dryFlag = if ($Apply) { "False" } else { "True" }
$pyScript = @"
import json, os
from app.db import SessionLocal
from app.services.trading.venue.coinbase_orphan_adopt import adopt_coinbase_orphan_stops

db = SessionLocal()
try:
    report = adopt_coinbase_orphan_stops(db, dry_run=$dryFlag)
finally:
    db.rollback()
    db.close()
print(json.dumps(report, indent=2, default=str))
"@

# Write the python entry to a temp file so PowerShell quoting doesn't
# mangle the multi-line script. Encoded ASCII (no BOM) per advisor
# brief 2.2.
$pyTmp = "scripts/_coinbase_orphan_adopt_runner.py"
[System.IO.File]::WriteAllBytes($pyTmp, [System.Text.Encoding]::ASCII.GetBytes($pyScript))

conda run -n chili-env python $pyTmp 2>&1 | Tee-Object -FilePath $out -Append
Remove-Item $pyTmp -ErrorAction SilentlyContinue

# ── Post-adoption state snapshot ─────────────────────────────────────
"" | Add-Content $out
"---DB: open Coinbase trades + bracket_intent state (AFTER)---" | Add-Content $out
psql -h localhost -p 5433 -U chili -d chili -c "$sqlBefore" 2>&1 | Out-String | Add-Content $out

"" | Add-Content $out
$elapsed = (Get-Date) - $start
("===== END $mode (took {0}) =====" -f $elapsed) | Add-Content $out
Write-Output "done ($mode); see $out"
