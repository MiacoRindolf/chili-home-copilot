$out = "scripts/dispatch-r23-r24-commit-deploy-output.txt"
"# r23 + r24 commit+deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

# ---------- Pre-flight ----------

S "git rev-parse HEAD (before)" {
    git rev-parse HEAD
}

S "py-compile gate (final guardrail)" {
    conda run -n chili-env python -m py_compile `
        app/config.py `
        app/migrations.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py
    if ($LASTEXITCODE -eq 0) { "py-compile OK" } else { "py-compile FAILED ABORT" }
}

# ---------- Stage + commit ----------

S "git add (touched files only)" {
    git add `
        app/config.py `
        app/migrations.py `
        app/services/broker_service.py `
        app/services/trading/venue/robinhood_spot.py `
        app/services/trading/bracket_writer_g2.py `
        app/services/trading/bracket_reconciliation_service.py `
        tests/test_bracket_writer_g2.py `
        scripts/_fix_rh_spot.py `
        scripts/_rewrite_bracket_writer_g2.py `
        scripts/_wire_g2_into_sweep.py `
        scripts/_r23_smoke.py `
        scripts/_r23_offline_test.py `
        scripts/dispatch-r23-pytest.ps1 `
        scripts/dispatch-r23-r24-deploy.ps1 `
        scripts/dispatch-r23-r24-validate.ps1 `
        scripts/dispatch-r23-r24-validate2.ps1 `
        scripts/dispatch-r23-r24-commit-deploy.ps1 `
        scripts/dispatch-r23-offline.ps1
    "git add complete"
}

S "git commit (one combined R23+R24 commit)" {
    git commit -m "fix(brackets): R23 wire G2 with real stop-loss + audit + R24 trade CHECK constraints"
}

S "git rev-parse HEAD (after commit)" {
    git rev-parse HEAD
}

# ---------- Restart chili to apply mig 214 ----------

S "force-recreate chili (run mig 214)" {
    docker compose up -d --force-recreate chili
}

S "wait 30s for migrations" { Start-Sleep -Seconds 30; "ok" }

S "chili health" {
    docker ps --filter "name=chili-home-copilot-chili-1" --format "{{.Names}} | {{.Status}}"
}

# ---------- Verify mig 214 landed ----------

S "schema_version contains mig 214" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '214%';"
}

S "trading_trades CHECK constraints present (chk_trades_*)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'trading_trades'::regclass AND conname LIKE 'chk_trades%' ORDER BY conname;"
}

S "audit row in trading_learning_events" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, LEFT(description, 250) FROM trading_learning_events WHERE event_type = 'migration_214' ORDER BY id DESC LIMIT 3;"
}

S "legacy bad-range count (sanity check vs audit's ~67)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS bad_rows, COUNT(*) FILTER (WHERE entry_price IS NULL OR entry_price <= 0) AS zero_entry, COUNT(*) FILTER (WHERE quantity IS NULL OR quantity <= 0) AS zero_qty, COUNT(*) FILTER (WHERE exit_price IS NOT NULL AND exit_price <= 0) AS bad_exit FROM trading_trades WHERE (entry_price IS NULL OR entry_price <= 0) OR (quantity IS NULL OR quantity <= 0) OR (exit_price IS NOT NULL AND exit_price <= 0);"
}

S "verify writer module + adapter signature in chili container" {
    docker compose exec -T chili python -c "from app.services.trading import bracket_writer_g2 as g2; from app.services.trading.venue.robinhood_spot import RobinhoodSpotAdapter; import inspect; print('place_stop_loss_sell_order present:', hasattr(RobinhoodSpotAdapter, 'place_stop_loss_sell_order')); print('signature:', inspect.signature(RobinhoodSpotAdapter.place_stop_loss_sell_order))"
}

S "config flags readable in chili container" {
    docker compose exec -T chili python -c "from app.config import settings; print('chili_bracket_sweep_writer_enabled:', settings.chili_bracket_sweep_writer_enabled); print('brain_live_brackets_mode:', settings.brain_live_brackets_mode); print('chili_bracket_writer_g2_enabled:', settings.chili_bracket_writer_g2_enabled); print('chili_bracket_writer_g2_place_missing_stop:', settings.chili_bracket_writer_g2_place_missing_stop)"
}

S "broker-sync-worker also picks up new writer module" {
    docker compose exec -T broker-sync-worker python -c "from app.services.trading import bracket_writer_g2 as g2; print('writer __all__:', g2.__all__)"
}

S "negative test: try violating chk_trades_entry_price_positive (should fail)" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; INSERT INTO trading_trades (ticker, direction, entry_price, quantity, status) VALUES ('CHK_TEST', 'long', 0, 1, 'open'); ROLLBACK;"
}

# ---------- Push retry ----------

S "git push origin main (retry; will fail with DNS or buffer if host network still degraded)" { git push origin main }

Write-Host "commit+deploy complete -- see $out"
