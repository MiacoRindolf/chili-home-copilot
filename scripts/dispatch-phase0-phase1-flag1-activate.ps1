$out = "scripts/dispatch-phase0-phase1-flag1-activate-output.txt"
"# Phase 0 verify + Phase 1 Flag 1 activation $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Phase 0: R23 stability ----------

S "P0: chili containers up?" {
    docker ps --filter "name=chili-home-copilot" --format "{{.Names}} | {{.Status}}"
}

S "P0: R23 sweep mode + agree count last 30 min" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, kind, COUNT(*) FROM trading_bracket_reconciliation_log WHERE observed_at > NOW() - INTERVAL '30 minutes' GROUP BY mode, kind ORDER BY count DESC;"
}

S "P0: g2_ events count last 30 min (should be 0 in steady-state)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_execution_events WHERE event_type LIKE 'g2_%' AND recorded_at > NOW() - INTERVAL '30 minutes';"
}

S "P0: ADT stop still resting at broker?" {
    docker compose exec -T chili python -c "from app.services import broker_service; o = broker_service.get_order_by_id('69f3947a-61cf-4e11-99c4-1f45879749e0'); print('state=', (o or {}).get('state'), 'stop_price=', (o or {}).get('stop_price'), 'qty=', (o or {}).get('quantity'))"
}

S "P0: brain_batch_jobs running count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT status, COUNT(*) FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY count DESC;"
}

S "P0: monitor_exit decisions last hour (R26 cooldown still working?)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT decision, COUNT(*) FROM trading_autotrader_runs WHERE created_at > NOW() - INTERVAL '1 hour' AND decision LIKE 'monitor_exit%' GROUP BY decision ORDER BY count DESC;"
}

# ---------- Phase 1 Flag 1 pre-flight ----------

S "P1F1 pre-flight: migrations 183 + 184 applied?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id IN ('183_pattern_survival_meta_classifier', '184_seed_hyperliquid_perp_contracts') ORDER BY version_id;"
}

S "P1F1 pre-flight: kill switch status (must be inactive)" {
    docker compose exec -T chili python -c "from app.services.trading.governance import get_kill_switch_status; print(get_kill_switch_status())"
}

S "P1F1 pre-flight: drawdown breaker status" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT * FROM trading_risk_state ORDER BY id DESC LIMIT 1;"
}

S "P1F1 pre-flight: pattern_survival_features table exists?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT column_name FROM information_schema.columns WHERE table_name = 'pattern_survival_features' ORDER BY ordinal_position;"
}

S "P1F1 pre-flight: current promoted/live + challenged pattern count (target audience)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT lifecycle_stage, COUNT(*) FROM scan_patterns WHERE lifecycle_stage IN ('live','challenged','promoted') GROUP BY lifecycle_stage ORDER BY count DESC;"
}

# ---------- Phase 1 Flag 1: ACTIVATE ----------

S "P1F1: append CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=true to .env" {
    $envFile = ".env"
    $current = if (Test-Path $envFile) { Get-Content $envFile } else { @() }
    $hasFlag = $current | Select-String "^CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=" -Quiet
    if ($hasFlag) {
        $current = $current | ForEach-Object {
            if ($_ -match "^CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=") {
                "CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=true"
            } else { $_ }
        }
        "REPLACED existing flag value -> true"
    } else {
        $current += "# Phase 1 Flag 1 (2026-04-30): activate pattern survival classifier feature collection"
        $current += "CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=true"
        "ADDED CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED=true"
    }
    $current | Set-Content $envFile -Encoding utf8
    Get-Content $envFile | Select-String "CHILI_PATTERN_SURVIVAL_CLASSIFIER_ENABLED|BRAIN_LIVE_BRACKETS_MODE|CHILI_BRACKET_SWEEP_WRITER_ENABLED" | ForEach-Object { $_.Line }
}

S "P1F1: restart chili (the daily job runs in chili web role per scheduler config)" {
    docker compose restart chili
}

S "P1F1: wait 15s for restart" { Start-Sleep -Seconds 15; "ok" }

S "P1F1: chili health" {
    docker ps --filter "name=chili-home-copilot-chili-1" --format "{{.Names}} | {{.Status}}"
}

S "P1F1: verify flag loaded in container" {
    docker compose exec -T chili python -c "from app.config import settings; print('classifier_enabled:', getattr(settings, 'chili_pattern_survival_classifier_enabled', None))"
}

S "P1F1: scheduler-worker has the pattern_survival_snapshot job registered?" {
    docker compose exec -T scheduler-worker python -c "import os; print('role=', os.environ.get('CHILI_SCHEDULER_ROLE'))"
    docker compose logs --tail=200 scheduler-worker 2>&1 | Select-String "pattern_survival" | Select-Object -Last 5
}

S "P1F1: try invoking the snapshot job manually (validates code path now, not 03:30 PT)" {
    docker compose exec -T scheduler-worker python -c @"
from app.db import SessionLocal
try:
    from app.services.trading.pattern_survival_features import run_daily_snapshot
    db = SessionLocal()
    try:
        out = run_daily_snapshot(db)
        print('manual run output:', out)
    finally:
        db.close()
except ImportError as e:
    print('ImportError - module path may differ:', e)
    # Try alternative import path
    try:
        from app.services.trading import pattern_survival_classifier as psc
        print('found pattern_survival_classifier module:', dir(psc))
    except Exception as e2:
        print('also failed:', e2)
"@
}

S "P1F1: row count after manual run" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*), MAX(snapshot_date) FROM pattern_survival_features;"
}

Write-Host "Phase 0 + Phase 1 Flag 1 dispatch done -- see $out"
