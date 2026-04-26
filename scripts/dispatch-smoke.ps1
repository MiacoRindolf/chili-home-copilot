# PowerShell smoke test for Phase D.1
param(
    [int]$WaitSeconds = 90
)
$ErrorActionPreference = "Stop"
$env:PGPASSWORD = "chili"
Write-Host "Waiting ${WaitSeconds}s for at least one cycle..."
Start-Sleep -Seconds $WaitSeconds
Write-Host ""
Write-Host "Breakdown (decision / count, last 5 min):"
docker compose exec -T postgres psql -U chili -d chili -c "SELECT COALESCE(decision, 'unknown') AS decision, COUNT(*) FROM code_agent_runs WHERE started_at > NOW() - INTERVAL '5 minutes' GROUP BY 1 ORDER BY 1;" 2>&1
$out = docker compose exec -T postgres psql -U chili -d chili -t -A -c "SELECT COUNT(*) FROM code_agent_runs WHERE started_at > NOW() - INTERVAL '5 minutes';" 2>&1
$total = [int](($out | ForEach-Object { "$_" }) -join "").Trim()
Write-Host "Total cycle rows: $total"
if ($total -lt 1) {
    Write-Host "FAIL: no cycle rows in last 5 min." -ForegroundColor Red
    Write-Host "  - Is CHILI_DISPATCH_ENABLED=1 in scheduler-worker env?" -ForegroundColor Yellow
    Write-Host "  - Did you rebuild after editing scripts/? (docker compose build chili)" -ForegroundColor Yellow
    Write-Host "  - Check: docker compose logs scheduler-worker (filter code_dispatch)" -ForegroundColor Yellow
    exit 1
}
Write-Host "OK: $total total cycle rows" -ForegroundColor Green
