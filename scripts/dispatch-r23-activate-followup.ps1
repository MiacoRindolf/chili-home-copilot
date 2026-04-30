$out = "scripts/dispatch-r23-activate-followup-output.txt"
"# r23 activate followup $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "now()" { docker compose exec -T postgres psql -U chili -d chili -c "SELECT NOW();" }

S "broker-sync-worker uptime" {
    docker ps --filter "name=chili-home-copilot-broker-sync-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "container env: do BRAIN_LIVE_BRACKETS_MODE + CHILI_BRACKET_SWEEP_WRITER_ENABLED match .env?" {
    docker compose exec -T broker-sync-worker sh -c 'env | grep -E "BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED"'
}

S "settings inside running broker-sync-worker process (live read)" {
    docker compose exec -T broker-sync-worker python -c "from app.config import settings; print('mode:', settings.brain_live_brackets_mode); print('sweep_writer:', settings.chili_bracket_sweep_writer_enabled); print('g2:', settings.chili_bracket_writer_g2_enabled); print('miss:', settings.chili_bracket_writer_g2_place_missing_stop)"
}

S "ALL bracket_reconciliation sweeps in the last 5 min (with timestamps)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, severity, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '5 minutes' GROUP BY observed_at, mode, kind, severity ORDER BY observed_at DESC LIMIT 30;"
}

S "writer execution events ever (cumulative)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT event_type, status, COUNT(*) FROM trading_execution_events WHERE event_type LIKE 'g2_%' GROUP BY event_type, status ORDER BY event_type;"
}

S "broker-sync-worker last 100 lines (look for writer_action / bracket_writer_g2)" {
    docker compose logs --tail=200 broker-sync-worker 2>&1 | Select-String -Pattern "bracket_writer_g2|bracket_reconciliation_ops|writer_action|missing_stop|authoritative|chili_bracket_sweep" | Select-Object -Last 60
}

# Wait 80s and recheck — gives 1+ sweep cycles after this dispatch
S "sleep 80s for one fresh sweep" { Start-Sleep -Seconds 80; "ok" }

S "fresh sweeps post-wait" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT observed_at, mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '90 seconds' GROUP BY observed_at, mode, kind ORDER BY observed_at DESC;"
}

S "g2_ events post-wait" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, status, ticker, payload_json->>'reason' AS reason, recorded_at FROM trading_execution_events WHERE event_type LIKE 'g2_%' ORDER BY id DESC LIMIT 20;"
}

S "broker-sync-worker very recent log lines" {
    docker compose logs --since 2m broker-sync-worker 2>&1 | Select-String -Pattern "bracket|authoritative|sweep_writer|g2_" | Select-Object -Last 30
}

Write-Host "followup complete -- see $out"
