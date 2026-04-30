$out = "scripts/dispatch-commit-round11-output.txt"
"# round-11 commit $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add scripts/dispatch-backfill-fake-flat-trades.ps1 scripts/_commit_msg_round11.txt scripts/dispatch-commit-round11.ps1
    git commit -F scripts/_commit_msg_round11.txt
}

S "verify" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, entry_price, exit_price, pnl FROM trading_trades WHERE id IN (393, 440, 585, 610, 611) ORDER BY id;"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, LEFT(description, 100) FROM trading_learning_events WHERE event_type='fake_flat_pnl_backfill';"
}

S "git push" {
    git push origin main
}

Write-Host "done"
