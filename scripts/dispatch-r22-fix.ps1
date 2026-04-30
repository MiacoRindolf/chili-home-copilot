$out = "scripts/dispatch-r22-fix-output.txt"
"# r22 fix $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1 | Out-String) | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "git add + amend commit (R22 still single commit)" {
    git add app/migrations.py scripts/dispatch-r22-fix.ps1
    git commit --amend --no-edit
}

S "force-recreate chili (run mig 213 fresh)" {
    docker compose up -d --force-recreate chili
}

S "wait 25s for migrations" { Start-Sleep -Seconds 25; "ok" }

S "chili health (should be Up + healthy)" {
    docker ps --filter "name=chili-home-copilot-chili-1" --format "{{.Names}} | {{.Status}}"
}

S "verify mig 213 applied" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT version_id, applied_at FROM schema_version WHERE version_id LIKE '213%';"
}

S "verify 4 patterns demoted" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, lifecycle_stage, promotion_status, ROUND(avg_return_pct::numeric,3) AS arp FROM scan_patterns WHERE id IN (860, 981, 1004, 1006) ORDER BY id;"
}

S "audit log entries from mig 213" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, LEFT(description, 150) AS desc, created_at FROM trading_learning_events WHERE event_type IN ('pattern_demoted_neg_ev', 'migration_213') ORDER BY id DESC LIMIT 10;"
}

S "any promoted with negative arp left? (expect 0)" {
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) FROM scan_patterns WHERE lifecycle_stage='promoted' AND avg_return_pct < 0;"
}

S "git push (retry)" { git push origin main }

Write-Host "done"
