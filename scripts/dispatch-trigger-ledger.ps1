# Trigger the pattern_regime_ledger run inside the chili container,
# verify the new mode='live' tagging.
$out = "scripts/dispatch-trigger-ledger-output.txt"
"# trigger ledger $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "before: rows by mode" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, COUNT(*) FROM trading_pattern_regime_performance_daily WHERE as_of_date >= CURRENT_DATE - INTERVAL '7 days' GROUP BY 1 ORDER BY 1;"
}

S "trigger build_ledger() in chili" {
    docker compose exec -T chili python -c "from app.db import SessionLocal; from app.services.trading.pattern_regime_ledger import build_ledger; db = SessionLocal(); r = build_ledger(db); print('result:', r); db.close()" 2>&1
}

S "after: rows by mode (with TODAY filter)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, COUNT(*), MAX(computed_at) FROM trading_pattern_regime_performance_daily WHERE as_of_date >= CURRENT_DATE - INTERVAL '1 day' GROUP BY 1 ORDER BY 1;"
}

S "sample of newly-tagged 'live' rows" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT pattern_id, regime_dimension, regime_label, n_trades, n_wins, mode, computed_at FROM trading_pattern_regime_performance_daily WHERE mode='live' ORDER BY computed_at DESC LIMIT 8;"
}

Write-Host "done"
