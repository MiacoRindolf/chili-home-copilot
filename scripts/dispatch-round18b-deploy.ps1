$out = "scripts/dispatch-round18b-deploy-output.txt"
"# round-18b deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git add + commit docker-compose change" {
    git add docker-compose.yml scripts/dispatch-round18b-deploy.ps1
    git commit -m "fix(brain-worker): mount ./scripts so brain_worker.py changes apply" -m "R18 deploy revealed that scripts/ was baked into the chili-app image, so script edits never loaded. ./app:/app/app is bind-mounted but ./scripts was not. Adding it makes brain_worker.py edits hot-reload on container restart, consistent with the existing app/ pattern."
}

S "force-recreate brain-worker (will pick up new mount + new code)" {
    docker compose up -d --force-recreate brain-worker
}

S "wait 10s for container start" {
    Start-Sleep -Seconds 10
    "ok"
}

S "container line count (expect 1636)" {
    docker compose exec -T brain-worker wc -l /app/scripts/brain_worker.py
}

S "container has audit funcs?" {
    docker compose exec -T brain-worker grep -c "_learning_cycle_audit_begin" /app/scripts/brain_worker.py
}

S "wait 60s for cycle to begin and audit_begin to fire" {
    Start-Sleep -Seconds 60
    "ok"
}

S "learning_cycle rows present?" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, status, started_at, ended_at, COALESCE(error_message, '') AS err, meta_json FROM brain_batch_jobs WHERE job_type='learning_cycle' ORDER BY started_at DESC LIMIT 3;"
}

S "any audit_begin/finish failure warnings?" {
    docker compose logs --since 5m brain-worker 2>$null | Select-String -Pattern "learning_cycle audit (begin|finish) failed" | Measure-Object | Select-Object -ExpandProperty Count
}

S "git push" {
    git push origin main
}

Write-Host "done"
