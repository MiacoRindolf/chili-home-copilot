# DOT-USD trade 1810 manual cleanup script.
#
# IMPORTANT: Operator should run this ONLY after confirming via the
# Robinhood app what happened to the entry order:
#   broker_order_id = 69f46dad-8712-4350-afc5-caf8875e0639
#
# Three scenarios:
#
# (A) Order NEVER filled (most likely given last_fill_at is NULL).
#     Use Path A.
#
# (B) Order filled and you sold the position manually at price $X.
#     Use Path B with that exit price.
#
# (C) Order filled and you still hold it on Robinhood somehow.
#     DO NOT RUN this script. Instead investigate why the broker API
#     reports zero quantity. Could be RH crypto API drift.
#
# This script is INTENTIONALLY commented out. Operator must:
#   1. Pick the path (A or B)
#   2. Uncomment that block
#   3. (Optionally) edit the EXIT_PRICE for Path B
#   4. Run the script

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "$PSScriptRoot\d-fix-1810-out.txt"
"# d-fix-1810 $(Get-Date -Format o)" | Out-File $out -Encoding utf8

"# pre-flight: show current state of trade 1810" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, ticker, status, quantity, entry_price, exit_price, exit_reason, broker_order_id, last_fill_at, entry_date, exit_date FROM trading_trades WHERE id=1810;" 2>&1 | Add-Content $out

# ===== PATH A: entry never filled (most likely) =====
# Marks the trade as cancelled with no realized PnL. Closes the
# bracket intent so the reconciler stops warning every minute.
#
# UNCOMMENT TO RUN:
#
# "# PATH A: marking trade 1810 as entry_never_filled" | Add-Content $out
# docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "BEGIN; UPDATE trading_trades SET status='cancelled', exit_date=NOW(), exit_reason='entry_never_filled', exit_price=NULL, pnl=NULL WHERE id=1810 AND status='open'; UPDATE trading_bracket_intents SET intent_state='abandoned', last_diff_reason='trade_cancelled_entry_never_filled', updated_at=NOW() WHERE trade_id=1810 AND intent_state='intent'; COMMIT;" 2>&1 | Add-Content $out


# ===== PATH B: entry filled, you sold manually =====
# Replace EXIT_PRICE_HERE with the actual price you sold at on RH.
# pnl is computed as (exit - entry) * quantity for a long.
#
# UNCOMMENT AND EDIT TO RUN:
#
# $EXIT_PRICE = "EXIT_PRICE_HERE"  # e.g., "1.34" -- the price you sold at
# "# PATH B: marking trade 1810 as manually closed at $EXIT_PRICE" | Add-Content $out
# docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "BEGIN; UPDATE trading_trades SET status='closed', exit_date=NOW(), exit_reason='manual_close_operator', exit_price=$EXIT_PRICE, pnl=ROUND(($EXIT_PRICE - 1.21568548)::numeric * 248, 2) WHERE id=1810 AND status='open'; UPDATE trading_bracket_intents SET intent_state='resolved', last_diff_reason='trade_closed_manually', updated_at=NOW() WHERE trade_id=1810; COMMIT;" 2>&1 | Add-Content $out


"# post-fix state" | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, status, exit_reason, exit_price, pnl, exit_date FROM trading_trades WHERE id=1810;" 2>&1 | Add-Content $out
docker exec chili-home-copilot-postgres-1 psql -U chili -d chili -P pager=off -c "SELECT id, intent_state, last_diff_reason, updated_at FROM trading_bracket_intents WHERE trade_id=1810;" 2>&1 | Add-Content $out

"# end" | Add-Content $out
