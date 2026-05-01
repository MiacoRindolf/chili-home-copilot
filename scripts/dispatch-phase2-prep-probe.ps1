$out = "scripts/dispatch-phase2-prep-probe-output.txt"
"# Phase 2 prep: probe execution cost + venue truth state $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "current mode flags" {
    docker compose exec -T chili python -c "from app.config import settings; print('execution_cost_mode:', settings.brain_execution_cost_mode); print('venue_truth_mode:', settings.brain_venue_truth_mode); print('position_sizer_mode:', settings.brain_position_sizer_mode); print('risk_dial_mode:', settings.brain_risk_dial_mode); print('capital_reweight_mode:', settings.brain_capital_reweight_mode)"
}

S "trading_execution_cost_estimates: row counts and freshness" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT side, window_days, COUNT(*) AS rows, MAX(last_updated_at) AS most_recent, MIN(last_updated_at) AS oldest FROM trading_execution_cost_estimates GROUP BY side, window_days ORDER BY rows DESC LIMIT 10;"
}

S "trading_execution_cost_estimates: top 10 most-active tickers" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT ticker, side, window_days, ROUND(median_spread_bps::numeric,2) AS med_spread, ROUND(p90_spread_bps::numeric,2) AS p90_spread, ROUND(median_slippage_bps::numeric,2) AS med_slip, sample_trades, last_updated_at FROM trading_execution_cost_estimates ORDER BY sample_trades DESC LIMIT 10;"
}

S "trading_venue_truth_log: row counts by mode + paper/live" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, paper_bool, COUNT(*) AS rows, MAX(created_at) AS most_recent FROM trading_venue_truth_log GROUP BY mode, paper_bool ORDER BY rows DESC;"
}

S "trading_venue_truth_log: realized vs expected divergence (recent paper trades)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS n, ROUND(AVG(expected_cost_fraction*10000)::numeric,2) AS avg_exp_bps, ROUND(AVG(realized_cost_fraction*10000)::numeric,2) AS avg_real_bps, ROUND(AVG((realized_cost_fraction - expected_cost_fraction)*10000)::numeric,2) AS avg_divergence_bps FROM trading_venue_truth_log WHERE created_at > NOW() - INTERVAL '7 days' AND expected_cost_fraction IS NOT NULL AND realized_cost_fraction IS NOT NULL;"
}

S "execution_cost_builder mode + flags" {
    docker compose exec -T chili python -c "from app.services.trading import execution_cost_builder as ecb; print('_ALLOWED_MODES:', ecb._ALLOWED_MODES); print('_effective_mode():', ecb._effective_mode()); print('mode_is_active():', ecb.mode_is_active())"
}

S "consumers of execution_cost_model -- where would 'authoritative' actually gate decisions?" {
    docker compose exec -T scheduler-worker python -c @"
import inspect
from app.services.trading import execution_cost_model as ecm
print('execution_cost_model functions:')
for name in dir(ecm):
    if not name.startswith('_'):
        obj = getattr(ecm, name)
        if callable(obj):
            print(' ', name)
"@
}

S "trading_position_sizer_log row count + freshness (Phase H)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS rows, MAX(created_at) AS most_recent FROM trading_position_sizer_log;"
}

S "schema_version: most recent migrations applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version ORDER BY applied_at DESC LIMIT 10;"
}

S "what consumes brain_execution_cost_mode in trading code (grep hits)" {
    docker compose exec -T chili sh -c 'grep -rn "brain_execution_cost_mode\|execution_cost_model.estimate" /app/app/services/trading/ 2>/dev/null | grep -v __pycache__ | head -15'
}

Write-Host "Phase 2 prep probe done -- see $out"
