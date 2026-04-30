$out = "scripts/dispatch-r23-r24-commit-fixup-output.txt"
"# r23 + r24 commit fixup $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale .git/index.lock if present" {
    if (Test-Path .git/index.lock) {
        Remove-Item -Force .git/index.lock
        "removed"
    } else {
        "no lock present"
    }
}

S "git status -s" { git status -s }

S "git add R23+R24 files" {
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
        scripts/dispatch-r23-r24-commit-fixup.ps1 `
        scripts/dispatch-r23-offline.ps1 `
        scripts/dispatch-r23-r24-validate-output.txt `
        scripts/dispatch-r23-r24-validate2-output.txt `
        scripts/dispatch-r23-r24-commit-deploy-output.txt `
        scripts/dispatch-r23-offline-output.txt
    "git add complete"
}

S "git status (post-add)" { git status -s }

S "git commit" {
    git commit -m "fix(brackets): R23 wire G2 with real stop-loss + audit + R24 trade CHECK constraints"
}

S "git rev-parse HEAD (after)" { git rev-parse HEAD }

S "git log --oneline -5" { git log --oneline -5 }

S "negative test: try violating chk_trades_entry_price_positive" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; INSERT INTO trading_trades (ticker, direction, entry_price, quantity, status) VALUES ('CHK_TEST_ZERO', 'long', 0, 1, 'open'); ROLLBACK;"
}

S "negative test: try violating chk_trades_quantity_positive" {
    docker compose exec -T postgres psql -U chili -d chili -c "BEGIN; INSERT INTO trading_trades (ticker, direction, entry_price, quantity, status) VALUES ('CHK_TEST_QTY', 'long', 100, 0, 'open'); ROLLBACK;"
}

S "config flags in chili container (single line python)" {
    docker compose exec -T chili python -c "from app.config import settings; print(repr({'sweep_writer': settings.chili_bracket_sweep_writer_enabled, 'mode': settings.brain_live_brackets_mode, 'g2_enabled': settings.chili_bracket_writer_g2_enabled}))"
}

S "broker-sync-worker writer module visible" {
    docker compose exec -T broker-sync-worker python -c "from app.services.trading import bracket_writer_g2 as g2; print(g2.__all__)"
}

S "git push origin main retry" { git push origin main }

Write-Host "fixup complete -- see $out"
