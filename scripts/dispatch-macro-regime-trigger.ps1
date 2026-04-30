# Trigger the macro_regime job manually and verify it writes a fresh snapshot.
# Also check whether the cron is registered in the live scheduler.
$out = "scripts/dispatch-macro-regime-trigger-output.txt"
"# Macro regime trigger $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "scheduler logs: macro registration?" {
    docker compose logs scheduler-worker --tail 500 2>&1 | Select-String -Pattern "macro_regime|Macro regime" | Select-Object -Last 20
}

S "live job list from chili (uses get_scheduler_info via API)" {
    docker compose exec -T chili python -c "from app.services.trading_scheduler import get_scheduler_info; import json; info = get_scheduler_info(); jobs = info.get('jobs', []); print(f'total jobs: {len(jobs)}'); [print(j['id'], '|', j['name'][:80]) for j in jobs if 'macro' in j['id'].lower() or 'regime' in j['id'].lower() or 'breadth' in j['id'].lower()]" 2>&1
}

S "scheduler-worker job list" {
    docker compose exec -T scheduler-worker python -c "from app.services.trading_scheduler import get_scheduler_info; info = get_scheduler_info(); jobs = info.get('jobs', []); print(f'total jobs: {len(jobs)}'); [print(j['id'], '|', j['name'][:80], '|', j.get('next_run_time','?')) for j in jobs if 'macro' in j['id'].lower() or 'regime' in j['id'].lower() or 'breadth' in j['id'].lower()]" 2>&1
}

S "trigger macro snapshot manually" {
    docker compose exec -T chili python -c "from app.db import SessionLocal; from app.services.trading.macro_regime_service import compute_and_persist; db = SessionLocal(); row = compute_and_persist(db); print('result:', row.regime_id if row else None, getattr(row,'macro_label',None), getattr(row,'coverage_score',None)); db.close()" 2>&1
}

S "post-trigger: macro snapshot freshness" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT MAX(as_of_date), MAX(computed_at), COUNT(*) FROM trading_macro_regime_snapshots WHERE as_of_date = CURRENT_DATE;"
}

Write-Host "done"
