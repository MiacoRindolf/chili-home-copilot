$out = "scripts/dispatch-cadence-investigation-output.txt"
"# trading cadence investigation $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- 1. Hold-duration distribution ----------

S "1a. crypto trade hold durations (last 30 days)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT broker_source, COUNT(*) AS n, ROUND(AVG(EXTRACT(EPOCH FROM (exit_date - entry_date))/60)::numeric,2) AS avg_minutes, ROUND(MIN(EXTRACT(EPOCH FROM (exit_date - entry_date))/60)::numeric,2) AS min_min, ROUND(MAX(EXTRACT(EPOCH FROM (exit_date - entry_date))/60)::numeric,2) AS max_min, ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (exit_date - entry_date))/60)::numeric,2) AS median_min FROM trading_trades WHERE status='closed' AND entry_date IS NOT NULL AND exit_date IS NOT NULL AND entry_date > NOW() - INTERVAL '30 days' GROUP BY broker_source ORDER BY n DESC;"
}

S "1b. crypto trade hold buckets (last 30 days)" {
    docker compose exec -T postgres psql -U chili -d chili -c "WITH t AS (SELECT EXTRACT(EPOCH FROM (exit_date - entry_date))/60 AS hold_min FROM trading_trades WHERE status='closed' AND broker_source='robinhood' AND ticker LIKE '%-USD' AND entry_date > NOW() - INTERVAL '30 days') SELECT CASE WHEN hold_min < 5 THEN 'a) <5min' WHEN hold_min < 30 THEN 'b) 5-30min' WHEN hold_min < 240 THEN 'c) 30min-4h' WHEN hold_min < 1440 THEN 'd) 4-24h' WHEN hold_min < 4320 THEN 'e) 1-3 days' ELSE 'f) >3 days' END AS bucket, COUNT(*) FROM t GROUP BY bucket ORDER BY bucket;"
}

# ---------- 2. Entry-attempt frequency ----------

S "2. autotrader decisions last 7 days (where does entry attempt land?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY decision ORDER BY count DESC LIMIT 20;"
}

S "2b. autotrader entry-blocked reasons last 7 days" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, reason, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '7 days' AND decision IN ('entry_blocked','entry_skipped','entry_filtered','entry_rejected','entry_deferred') GROUP BY decision, reason ORDER BY count DESC LIMIT 25;"
}

# ---------- 3. Pattern-imminent alert cadence ----------

S "3a. pattern-imminent alert volume by asset class (last 7 days)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT CASE WHEN ticker LIKE '%-USD' THEN 'crypto' ELSE 'equity' END AS asset_class, COUNT(*), MIN(created_at) AS oldest, MAX(created_at) AS newest FROM trading_breakout_alerts WHERE created_at > NOW() - INTERVAL '7 days' GROUP BY asset_class;"
}

S "3b. pattern firing distribution last 24h - top patterns by alert count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT scan_pattern_id, COUNT(*) AS alerts, MIN(created_at) AS first, MAX(created_at) AS last FROM trading_breakout_alerts WHERE created_at > NOW() - INTERVAL '24 hours' AND scan_pattern_id IS NOT NULL GROUP BY scan_pattern_id ORDER BY alerts DESC LIMIT 15;"
}

# ---------- 4. Crypto-specific scanner cadence ----------

S "4. crypto_breakout_scanner job runs last 24h" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, COUNT(*) AS runs, MAX(started_at) AS most_recent, AVG(EXTRACT(EPOCH FROM (ended_at - started_at))) AS avg_duration_sec FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' AND job_type IN ('crypto_breakout_scanner','momentum_scanner','pattern_imminent_scanner','autotrader_tick','crypto_stop_monitor') GROUP BY job_type ORDER BY job_type;"
}

# ---------- 5. Cooldowns / throttles ----------

S "5. autotrader cooldown / throttle settings" {
    docker compose exec -T chili python -c "from app.config import settings; import re; attrs = [a for a in dir(settings) if re.search(r'cooldown|throttle|min_hold|min_interval|interval_seconds|interval_minutes|min_entry_gap|max_open|reentry|debounce', a, re.I)]; [print(f'{a} = {getattr(settings, a)!r}') for a in attrs[:40]]"
}

S "5b. autotrader tick interval + max_instances" {
    docker compose exec -T chili sh -c 'grep -n -A 1 "autotrader_tick\|autotrader_monitor\|crypto_stop_monitor\|pattern_imminent" /app/app/services/trading_scheduler.py | grep -E "IntervalTrigger|seconds=|minutes=" | head -10'
}

# ---------- 6. Stop / target tightness ----------

S "6a. crypto stop/target distance from entry (last 30 days closed)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS n, ROUND(AVG((stop_loss - entry_price) / entry_price * 100)::numeric,2) AS avg_stop_pct, ROUND(AVG((take_profit - entry_price) / entry_price * 100)::numeric,2) AS avg_target_pct FROM trading_trades WHERE status='closed' AND ticker LIKE '%-USD' AND entry_date > NOW() - INTERVAL '30 days' AND stop_loss IS NOT NULL AND take_profit IS NOT NULL;"
}

S "6b. crypto exit_reason distribution last 30 days" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT exit_reason, COUNT(*) FROM trading_trades WHERE status='closed' AND ticker LIKE '%-USD' AND entry_date > NOW() - INTERVAL '30 days' GROUP BY exit_reason ORDER BY count DESC LIMIT 15;"
}

# ---------- 7. Governance gates currently blocking ----------

S "7a. kill switch + breaker state right now" {
    docker compose exec -T chili python -c "from app.services.trading.governance import get_kill_switch_status; print('kill_switch:', get_kill_switch_status())"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, snapshot_date, breaker_tripped, breaker_reason, regime FROM trading_risk_state ORDER BY id DESC LIMIT 3;"
}

S "7b. is the autotrader actually running ticks? (last 1h)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS tick_runs FROM brain_batch_jobs WHERE job_type LIKE 'autotrader%' AND started_at > NOW() - INTERVAL '1 hour';"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS decisions_1h FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '1 hour';"
}

# ---------- 8. Crypto patterns active ----------

S "8. promoted/live crypto-eligible patterns" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT lifecycle_stage, asset_class, COUNT(*) FROM scan_patterns WHERE lifecycle_stage IN ('promoted','live','challenged') GROUP BY lifecycle_stage, asset_class ORDER BY lifecycle_stage, asset_class;"
}

S "9. recent crypto trade examples (last 30 days, full pattern info)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, ROUND(EXTRACT(EPOCH FROM (exit_date - entry_date))/60::numeric,1) AS hold_min, ROUND(entry_price::numeric,4) AS entry, ROUND(exit_price::numeric,4) AS exit, pnl, exit_reason, scan_pattern_id, entry_date::date FROM trading_trades WHERE ticker LIKE '%-USD' AND entry_date > NOW() - INTERVAL '30 days' ORDER BY entry_date DESC LIMIT 20;"
}

Write-Host "cadence investigation done -- see $out"
