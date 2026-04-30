$out = "scripts/dispatch-r23-activate-output.txt"
"# r23 ACTIVATE live missing-stop repair $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Pre-snapshot ----------

S "open Robinhood trades (writer's targets)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, broker_source, status, broker_status, broker_order_id, ROUND(entry_price::numeric, 4) AS entry, ROUND(stop_loss::numeric, 4) AS stop, quantity, EXTRACT(EPOCH FROM (NOW() - entry_date))/3600 AS age_hours FROM trading_trades WHERE status = 'open' AND broker_source = 'robinhood' ORDER BY entry_date DESC;"
}

S "BracketIntent state for those trades" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT bi.id AS intent_id, bi.trade_id, t.ticker, bi.intent_state, ROUND(bi.stop_price::numeric, 4) AS stop, bi.last_observed_at FROM trading_bracket_intents bi JOIN trading_trades t ON t.id = bi.trade_id WHERE t.status = 'open' AND t.broker_source = 'robinhood' ORDER BY bi.id DESC;"
}

S "last 24h reconciliation kinds (what would the writer act on?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT kind, severity, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '24 hours' GROUP BY kind, severity ORDER BY COUNT(*) DESC;"
}

S "any prior g2_ execution_events (should be 0 -- writer never fired before)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS prior_g2_events FROM trading_execution_events WHERE event_type LIKE 'g2_%';"
}

# ---------- Flip the flags ----------

S "current .env entries for these flags (before)" {
    Get-Content .env | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

S "append flags to .env (idempotent)" {
    $envFile = ".env"
    $current = if (Test-Path $envFile) { Get-Content $envFile } else { @() }
    $needed = @(
        @{key="CHILI_BRACKET_SWEEP_WRITER_ENABLED"; value="1"},
        @{key="BRAIN_LIVE_BRACKETS_MODE"; value="authoritative"}
    )
    $changes = 0
    foreach ($n in $needed) {
        $line = "$($n.key)=$($n.value)"
        $existing = $current | Select-String "^$($n.key)=" -Quiet
        if ($existing) {
            # Replace existing
            $current = $current | ForEach-Object {
                if ($_ -match "^$($n.key)=") { $line } else { $_ }
            }
            "REPLACED $($n.key)"
            $changes++
        } else {
            $current += "# Round 23 (2026-04-30): activate live missing-stop repair"
            $current += $line
            "ADDED $($n.key)=$($n.value)"
            $changes++
        }
    }
    if ($changes -gt 0) {
        $current | Set-Content $envFile -Encoding utf8
    }
    "wrote $envFile, $changes changes"
}

S "current .env entries for these flags (after)" {
    Get-Content .env | Select-String "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

# ---------- Restart broker-sync-worker ----------

S "force-recreate broker-sync-worker (picks up .env)" {
    docker compose up -d --force-recreate broker-sync-worker
}

S "broker-sync-worker health" {
    Start-Sleep -Seconds 5
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "verify flags landed in container" {
    docker compose exec -T broker-sync-worker python -c "from app.config import settings; print(repr({'sweep_writer': settings.chili_bracket_sweep_writer_enabled, 'mode': settings.brain_live_brackets_mode, 'g2_enabled': settings.chili_bracket_writer_g2_enabled, 'place_missing_stop': settings.chili_bracket_writer_g2_place_missing_stop}))"
}

# ---------- Wait one sweep cycle ----------

S "wait 130s for at least one bracket reconciliation sweep" {
    Start-Sleep -Seconds 130
    "ok"
}

# ---------- Post-snapshot ----------

S "newest reconciliation sweep (mode should now be authoritative)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT sweep_id, mode, kind, severity, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '5 minutes' GROUP BY sweep_id, mode, kind, severity ORDER BY sweep_id DESC, kind LIMIT 30;"
}

S "g2_ execution_events created by writer in the last 5 minutes" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, broker_source, order_id, payload_json->>'reason' AS reason, payload_json->>'stop_price' AS stop_price, payload_json->>'qty' AS qty, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '5 minutes' ORDER BY id DESC LIMIT 50;"
}

S "broker-sync-worker logs (last 50 bracket_writer_g2 / bracket_reconciliation lines)" {
    docker compose logs --tail=400 broker-sync-worker 2>&1 | Select-String -Pattern "bracket_writer_g2|bracket_reconciliation_ops|writer_action|missing_stop" | Select-Object -Last 50
}

S "new broker stop orders (open SELL stops on robinhood since flip)" {
    docker compose exec -T chili python -c "from app.services import broker_service; orders = broker_service.get_recent_orders() or []; stops = [o for o in orders if str(o.get('side','')).lower()=='sell' and o.get('trigger') == 'stop' and o.get('state') in ('confirmed','queued','open','active')]; import json; print('count:', len(stops)); print('first 5:', json.dumps(stops[:5], default=str, indent=2))" 2>&1
}

# ---------- Final pulse ----------

S "open Robinhood trades (post-snapshot)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, broker_order_id, ROUND(entry_price::numeric, 4) AS entry, ROUND(stop_loss::numeric, 4) AS stop FROM trading_trades WHERE status = 'open' AND broker_source = 'robinhood' ORDER BY entry_date DESC;"
}

Write-Host "activate complete -- see $out"
