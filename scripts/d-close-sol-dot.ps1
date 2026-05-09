# Operator-approved: close trades 1810 (DOT) and 1824 (SOL) in DB
# with the actual exit prices captured from autotrader-worker logs.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-close-sol-dot-out.txt"
"# d-close-sol-dot $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: pre-state" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, exit_price, exit_reason, pnl FROM trading_trades WHERE id IN (1810, 1824);" 2>&1 | Add-Content $out

"# step 2: apply close SQL inside a transaction" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c @"
BEGIN;
UPDATE trading_trades
SET status='closed', exit_price=1.388, exit_date='2026-05-09 01:58:23+00', exit_reason='take_profit_hit',
    pnl=ROUND((1.388 - 1.21568548) * 248, 2)
WHERE id=1810 AND status='open';
UPDATE trading_trades
SET status='closed', exit_price=93.69, exit_date='2026-05-09 01:58:53+00', exit_reason='take_profit_hit',
    pnl=ROUND((93.69 - 84.22) * 6, 2)
WHERE id=1824 AND status='open';
COMMIT;
"@ 2>&1 | Add-Content $out

"# step 3: post-state" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, exit_price, exit_reason, exit_date, ROUND(pnl::numeric, 2) AS pnl FROM trading_trades WHERE id IN (1810, 1824);" 2>&1 | Add-Content $out

"# step 4: realized pnl summary" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT ticker, exit_reason, ROUND(pnl::numeric, 2) AS pnl FROM trading_trades WHERE id IN (1810, 1824);" 2>&1 | Add-Content $out

"# end" | Add-Content $out
