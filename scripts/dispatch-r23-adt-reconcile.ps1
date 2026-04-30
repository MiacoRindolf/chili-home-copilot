$out = "scripts/dispatch-r23-adt-reconcile-output.txt"
"# R23 ADT trade reconcile (out-of-sync after manual sale + stop cancel) $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Diagnose first: any other out-of-sync trades? ----------

S "all open Robinhood trades vs broker positions" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl FROM trading_trades WHERE status='open' AND broker_source='robinhood' ORDER BY id;"
}

S "broker positions ground truth (Robinhood)" {
    docker compose exec -T chili python -c "from app.services import broker_service; positions = broker_service.get_positions() or []; [print(f'{p.get(\"ticker\")}  qty={p.get(\"quantity\")}') for p in positions]"
}

# ---------- Fix ADT (1694) ----------

S "BEFORE: ADT trade row" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl, exit_reason, exit_date, pending_exit_status, pending_exit_reason FROM trading_trades WHERE id = 1694;"
}

S "BEFORE: bracket intent for trade 1694" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, trade_id, intent_state, ROUND(stop_price::numeric,4) AS stop, last_observed_at FROM trading_bracket_intents WHERE trade_id = 1694;"
}

S "reconcile: flip ADT trade 1694 to status=closed (broker exited at 20:00 UTC; pnl already set)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; UPDATE trading_trades SET status='closed', exit_reason=COALESCE(exit_reason, 'broker_external_exit'), exit_date=COALESCE(exit_date, '2026-04-30 20:00:16'::timestamp), notes=COALESCE(notes,'') || E'\n[r23-reconcile] status open->closed; user manually sold position at broker ~20:00 UTC and cancelled stop 69f3947a; pnl + exit_price already populated by broker_sync' WHERE id = 1694; SELECT id, status, broker_status, exit_reason, exit_date FROM trading_trades WHERE id=1694; COMMIT;"
}

S "reconcile: terminate the bracket intent (intent_state=reconciled)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; UPDATE trading_bracket_intents SET intent_state='reconciled' WHERE trade_id = 1694; SELECT id, trade_id, intent_state FROM trading_bracket_intents WHERE trade_id = 1694; COMMIT;"
}

S "audit row" {
    docker compose exec -T postgres psql -U chili -d chili -c "INSERT INTO trading_learning_events (user_id, event_type, description, created_at) VALUES (NULL, 'r23_external_exit_reconcile', 'Trade 1694 ADT reconciled status=open->closed after operator manually sold position + cancelled bracket stop 69f3947a around 20:00 UTC. Bracket intent terminated. Writer loop (60 events/30min on missing_stop classification with no broker position) should now stop on next sweep.', CURRENT_TIMESTAMP) RETURNING id;"
}

# ---------- Wait for one sweep cycle to verify ----------

S "wait 70s for next sweep" { Start-Sleep -Seconds 70; "ok" }

S "AFTER sweep: bracket reconciliation last 90s" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC LIMIT 5;"
}

S "AFTER sweep: g2_ events count last 90s (should be 0 -- no open trades to sweep)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS new_events FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '90 seconds';"
}

S "FINAL: open Robinhood trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status FROM trading_trades WHERE status='open' AND broker_source='robinhood';"
}

# ---------- Breaker note ----------

S "breaker context (informational only -- not auto-resetting)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT 'breaker tripped at 15:35 UTC on 5-consecutive-loss rule; daily realized pnl is only -3.90 USD (well under 300 cap); 5 losses are mostly synthetic broker_reconcile_position_gone exits with crude price estimates. OPERATOR DECISION whether to reset.' AS note;"
}

Write-Host "ADT reconcile done -- see $out"
