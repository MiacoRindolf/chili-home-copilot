$out = "scripts/dispatch-round12-deploy-output.txt"
"# round-12 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "remove stale lock" {
    if (Test-Path ".git/index.lock") { Remove-Item -Force ".git/index.lock"; "removed" } else { "none" }
}

S "git add + commit" {
    git add `
      app/migrations.py `
      app/config.py `
      app/services/trading/learning.py `
      app/services/trading/backtest_queue.py `
      app/services/trading/backtest_queue_priority.py `
      app/services/trading_scheduler.py `
      docker-compose.yml `
      scripts/_commit_msg_round12.txt `
      scripts/dispatch-round12-deploy.ps1 `
      scripts/dispatch-probe-bt-settings.ps1 `
      scripts/_commit_msg_round11.txt `
      scripts/dispatch-commit-round11.ps1 `
      scripts/dispatch-backfill-fake-flat-trades.ps1
    git commit -F scripts/_commit_msg_round12.txt
}

S "before: backtest_priority distribution" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FILTER (WHERE backtest_priority = 0) AS zero, COUNT(*) FILTER (WHERE backtest_priority > 0) AS nonzero FROM scan_patterns;"
}

S "recreate chili (mig 212) + scheduler-worker (new env + new cron)" {
    docker compose up -d --force-recreate chili scheduler-worker
}

S "wait + verify mig 212 + new column" {
    Start-Sleep -Seconds 30
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id FROM schema_version WHERE version_id LIKE '212%';"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT column_name FROM information_schema.columns WHERE table_name='scan_patterns' AND column_name='consecutive_zero_trade_runs';"
}

S "verify env: BRAIN_QUEUE_BACKTEST_EXECUTOR" {
    docker compose exec -T scheduler-worker env 2>&1 | Select-String -Pattern "BRAIN_QUEUE_BACKTEST_EXECUTOR|BRAIN_QUEUE_PROCESS_CAP"
}

S "trigger priority scorer manually for immediate effect" {
    docker compose exec -T chili python -c "from app.db import SessionLocal; from app.services.trading.backtest_queue_priority import run_priority_scoring; db=SessionLocal(); print(run_priority_scoring(db)); db.close()" 2>&1
}

S "after: backtest_priority distribution" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT CASE WHEN backtest_priority >= 50 THEN 'hi (>=50)' WHEN backtest_priority >= 10 THEN 'med (10-49)' WHEN backtest_priority > 0 THEN 'lo (1-9)' ELSE 'zero' END AS bucket, COUNT(*) FROM scan_patterns GROUP BY 1 ORDER BY 2 DESC;"
}

S "git push" {
    git push origin main
}

Write-Host "done"
