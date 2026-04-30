$out = "scripts/dispatch-r25-reconciler-fix-output.txt"
"# r25 reconciler fix + verify $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "py-compile" {
    conda run -n chili-env python -m py_compile app/services/trading/brain_batch_reconciler.py
    if ($LASTEXITCODE -eq 0) { "OK" } else { "FAILED" }
}

S "git status (sanity)" {
    git status -s app/services/trading/brain_batch_reconciler.py
}

S "force-recreate scheduler-worker (pick up reconciler fix)" {
    docker compose up -d --force-recreate scheduler-worker
}

S "wait 12s" { Start-Sleep -Seconds 12; "ok" }

S "scheduler-worker uptime" {
    docker ps --filter "name=chili-home-copilot-scheduler-worker-1" --format "{{.Names}} | {{.Status}}"
}

S "manually run the reconciler now (should orphan all 25 stale rows)" {
    docker compose exec -T scheduler-worker python -c "from app.db import SessionLocal; from app.services.trading.brain_batch_reconciler import reconcile_stale_batch_jobs; db = SessionLocal(); print(reconcile_stale_batch_jobs(db)); db.close()"
}

S "stale running rows AFTER manual reconcile" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT status, COUNT(*) FROM brain_batch_jobs WHERE started_at > NOW() - INTERVAL '24 hours' GROUP BY status ORDER BY count DESC;"
}

S "audit: most recent orphan rows + final_state_reason format" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, status, final_state_reason, orphaned_at FROM brain_batch_jobs WHERE status = 'orphaned' ORDER BY orphaned_at DESC LIMIT 5;"
}

S "wait 6 minutes for one scheduled reconciler run" {
    Start-Sleep -Seconds 320
    "ok"
}

S "scheduler-worker logs: brain_batch_reconciler runs in last 7 min" {
    docker compose logs --since 7m scheduler-worker 2>&1 | Select-String -Pattern "brain_batch_reconciler|reconciled.*orphaned|final_state_reason" | Select-Object -Last 15
}

S "any new running rows pile up since fix?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT job_type, COUNT(*), EXTRACT(EPOCH FROM (NOW() - MIN(started_at)))/60 AS oldest_min FROM brain_batch_jobs WHERE status = 'running' GROUP BY job_type ORDER BY oldest_min DESC;"
}

Write-Host "reconciler fix + verify done -- see $out"
