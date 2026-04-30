$out = "scripts/dispatch-round22-deploy-output.txt"
"# round-22 deploy $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git add + commit" {
    git add app/migrations.py scripts/_commit_msg_round22.txt scripts/dispatch-round22-deploy.ps1
    git commit -F scripts/_commit_msg_round22.txt
}

S "before: lifecycle distribution among the 4 target patterns" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, lifecycle_stage, promotion_status, ROUND(avg_return_pct::numeric,3) AS arp FROM scan_patterns WHERE id IN (860, 981, 1004, 1006) ORDER BY id;"
}

S "before: total promoted count" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS promoted_total FROM scan_patterns WHERE lifecycle_stage='promoted';"
}

S "force-recreate chili to run startup migrations" {
    docker compose up -d --force-recreate chili
}

S "wait 30s for migrations + healthcheck" {
    Start-Sleep -Seconds 30
    "ok"
}

S "verify mig 213 applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '213%';"
}

S "after: lifecycle distribution among the 4 target patterns (expect 'challenged')" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, lifecycle_stage, promotion_status, ROUND(avg_return_pct::numeric,3) AS arp FROM scan_patterns WHERE id IN (860, 981, 1004, 1006) ORDER BY id;"
}

S "after: total promoted count (expect 4 less)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS promoted_total FROM scan_patterns WHERE lifecycle_stage='promoted';"
}

S "after: any promoted with negative arp left? (expect 0)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM scan_patterns WHERE lifecycle_stage='promoted' AND avg_return_pct < 0;"
}

S "audit log entries from mig 213" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, scan_pattern_id, LEFT(message, 150) AS msg, created_at FROM trading_learning_events WHERE event_type IN ('pattern_demoted_neg_ev', 'migration_213') ORDER BY id DESC LIMIT 10;"
}

S "git push" { git push origin main }

Write-Host "done"
