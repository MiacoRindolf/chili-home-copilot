$out = "scripts/dispatch-r23-r24-deploy-output.txt"
"# r23 + r24 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Pre-flight ----------

S "git status -s (working tree)" {
    git status -s
}

S "py-compile gate (R23 + R24 touched files)" {
    conda run -n chili-env python -m py_compile `
        app/config.py `
        app/migrations.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "py-compile OK" } else { "py-compile FAILED ($LASTEXITCODE)" }
}

S "verify migration ids unique" {
    .\scripts\verify-migration-ids.ps1
}

# ---------- Pytest (the writer changes) ----------

S "pytest tests/test_bracket_writer_g2.py" {
    $env:TEST_DATABASE_URL = "postgresql://chili:chili@localhost:5433/chili_test"
    conda run -n chili-env python -m pytest tests/test_bracket_writer_g2.py -v
}

# ---------- Commit ----------

S "git add + commit (single round-23+24 commit)" {
    git add app/config.py app/migrations.py app/services/broker_service.py app/services/trading/venue/robinhood_spot.py app/services/trading/bracket_writer_g2.py app/services/trading/bracket_reconciliation_service.py tests/test_bracket_writer_g2.py scripts/_fix_rh_spot.py scripts/_rewrite_bracket_writer_g2.py scripts/_wire_g2_into_sweep.py scripts/dispatch-r23-pytest.ps1 scripts/dispatch-r23-r24-deploy.ps1
    git commit -m "fix(brackets): R23 wire G2 writer with real stop-loss + audit + R24 trade CHECK constraints"
}

# ---------- Restart to apply mig 214 ----------

S "force-recreate chili (run mig 214)" {
    docker compose up -d --force-recreate chili
}

S "wait 25s for migrations" { Start-Sleep -Seconds 25; "ok" }

S "chili health" {
    docker ps --filter "name=chili-home-copilot-chili-1" --format "{{.Names}} | {{.Status}}"
}

# ---------- Verify mig 214 landed ----------

S "schema_version contains mig 214" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '214%';"
}

S "trading_trades CHECK constraints present" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'trading_trades'::regclass AND conname LIKE 'chk_trades%' ORDER BY conname;"
}

S "audit row in trading_learning_events" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, LEFT(description, 200) FROM trading_learning_events WHERE event_type = 'migration_214' ORDER BY id DESC LIMIT 3;"
}

S "legacy bad-range count (should match audit's 67-ish)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM trading_trades WHERE entry_price IS NULL OR entry_price <= 0 OR quantity IS NULL OR quantity <= 0 OR (exit_price IS NOT NULL AND exit_price <= 0);"
}

S "verify writer module + adapter signature in chili container" {
    docker compose exec -T chili python -c "from app.services.trading import bracket_writer_g2 as g2; from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter; import inspect; print('place_stop_loss_sell_order:', hasattr(RobinhoodSpotAdapter, 'place_stop_loss_sell_order')); print('signature:', inspect.signature(RobinhoodSpotAdapter.place_stop_loss_sell_order))"
}

S "config flags readable in chili container" {
    docker compose exec -T chili python -c "from app.config import settings; print('chili_bracket_sweep_writer_enabled:', settings.chili_bracket_sweep_writer_enabled); print('brain_live_brackets_mode:', settings.brain_live_brackets_mode); print('chili_bracket_writer_g2_enabled:', settings.chili_bracket_writer_g2_enabled)"
}

# ---------- Push retry ----------

S "git push origin main (retry past Round 22 + new commit)" { git push origin main }

Write-Host "done -- see $out"
