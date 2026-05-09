# EMERGENCY REVERT: Phase E falsely cancelled 14 crypto trades that
# Robinhood actually has positions for. Restore them to status='open',
# clear the wrong exit_reason/exit_date, reset the streak counter,
# leave entry_price/quantity/broker_order_id intact.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-revert-14-out.txt"
"# d-revert-14 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# step 1: pre-revert state (these are the false cancellations)" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, exit_reason, exit_date FROM trading_trades WHERE id IN (1807,1808,1809,1810,1823,1824,1826,1827,1828,1831,1832,1835,1836,1837) ORDER BY id;" 2>&1 | Add-Content $out

"# step 2: REVERT all 14 to status='open' with original metadata intact" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "BEGIN; UPDATE trading_trades SET status='open', exit_date=NULL, exit_reason=NULL, exit_price=NULL, pnl=NULL, crypto_broker_zero_qty_streak=0 WHERE id IN (1807,1808,1809,1810,1823,1824,1826,1827,1828,1831,1832,1835,1836,1837) AND status='cancelled' AND exit_reason='entry_never_filled'; COMMIT;" 2>&1 | Add-Content $out

"# step 3: post-revert state" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, exit_reason, exit_date, last_fill_at, crypto_broker_zero_qty_streak FROM trading_trades WHERE id IN (1807,1808,1809,1810,1823,1824,1826,1827,1828,1831,1832,1835,1836,1837) ORDER BY id;" 2>&1 | Add-Content $out

"# step 4: DISABLE Phase E sweep until we fix the broker-API-trust issue" | Add-Content $out
"# Setting CHILI_CRYPTO_ENTRY_FILL_WINDOW_HOURS=0 disables Layer 1." | Add-Content $out
"# Setting CHILI_CRYPTO_BROKER_ZERO_QTY_STREAK_MIN=0 disables Layer 2." | Add-Content $out
"# Operator: add these to .env and restart, OR I can write the .env edit script." | Add-Content $out

"# step 5: also reset the breaker (in case Phase E's burst trip lingers)" | Add-Content $out
docker exec chili-home-copilot-chili-1 python -c @"
from app.services.trading.portfolio_risk import reset_breaker, is_breaker_tripped
print('chili breaker before reset:', is_breaker_tripped())
reset_breaker()
print('chili breaker after reset:', is_breaker_tripped())
"@ 2>&1 | Add-Content $out
docker exec chili-home-copilot-autotrader-worker-1 python -c @"
from app.services.trading.portfolio_risk import reset_breaker, is_breaker_tripped
print('autotrader-worker breaker before reset:', is_breaker_tripped())
reset_breaker()
print('autotrader-worker breaker after reset:', is_breaker_tripped())
"@ 2>&1 | Add-Content $out

"# end" | Add-Content $out
