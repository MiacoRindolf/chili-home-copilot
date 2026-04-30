$out = "scripts/dispatch-r23-pulse-and-survey-output.txt"
"# r23 pulse check + survey for next item $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- R23 stability ----------

S "all chili containers up?" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "bracket sweep distribution last 30 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, kind, severity, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '30 minutes' GROUP BY mode, kind, severity ORDER BY count DESC;"
}

S "g2_ events distribution last 30 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT event_type, status, COUNT(*) FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '30 minutes' GROUP BY event_type, status ORDER BY event_type;"
}

S "writer activity last 30 min (ADT stop still resting?)" {
    docker compose exec -T chili python -c "from app.services import broker_service; o = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0'); print('state=', (o or {}).get('state'), 'cum_qty=', (o or {}).get('cumulative_quantity'))"
}

# ---------- Survey: §7.1 brain_batch_jobs heartbeat ----------

S "stale running brain_batch_jobs (audit's HIGH §7.1)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, COUNT(*), MIN(started_at) AS oldest, MAX(started_at) AS newest, EXTRACT(EPOCH FROM (NOW() - MIN(started_at)))/60 AS oldest_minutes_ago FROM brain_batch_jobs WHERE status = 'running' GROUP BY job_type ORDER BY oldest_minutes_ago DESC;"
}

S "brain_batch_jobs status distribution last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT status, COUNT(*) FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY count DESC;"
}

S "brain_batch_jobs schema (does heartbeat_at column exist?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='brain_batch_jobs' AND column_name IN ('heartbeat_at','status','started_at','finished_at') ORDER BY ordinal_position;"
}

# ---------- Survey: §6.3 exit defer escalation ----------

S "exit-related autotrader decisions last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '24 hours' AND decision LIKE 'monitor_exit%' GROUP BY decision ORDER BY count DESC LIMIT 20;"
}

S "exit-deferred reasons distribution last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT reason, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '24 hours' AND decision = 'monitor_exit_deferred' GROUP BY reason ORDER BY count DESC LIMIT 10;"
}

S "exit-rejected reasons distribution last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT reason, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '24 hours' AND decision = 'monitor_exit_rejected' GROUP BY reason ORDER BY count DESC LIMIT 10;"
}

# ---------- Other open items ----------

S "any new audit alerts since R23 deploy?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT event_type, COUNT(*) FROM trading_learning_events WHERE created_at > NOW() - INTERVAL '2 hours' GROUP BY event_type ORDER BY count DESC LIMIT 10;"
}

S "open Robinhood trades summary" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, status, broker_status, ROUND(entry_price::numeric,4) AS entry FROM trading_trades WHERE status = 'open' AND broker_source = 'robinhood' ORDER BY id;"
}

Write-Host "pulse + survey done -- see $out"
