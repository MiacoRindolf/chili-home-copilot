# Rebuild chili-app:local image (so scripts/ changes ship) then force-recreate
# scheduler-worker. Use after any edit to scripts/scheduler_worker.py or other
# files outside ./app/ (which is bind-mounted and updates live).
#
# Usage: .\scripts\dispatch-recreate.ps1

$start = Get-Date
Write-Host "[1/3] Building chili-app:local (this is the slow step)..."
docker compose build chili 2>&1 | Select-String -Pattern "ERROR|FROM|COPY|CACHED|Built|naming" | Select-Object -Last 10
Write-Host ""

Write-Host "[2/3] Force-recreating scheduler-worker..."
docker compose up -d --force-recreate scheduler-worker
Start-Sleep -Seconds 5

Write-Host ""
Write-Host "[3/3] Tail of dispatch-related startup logs:"
Start-Sleep -Seconds 25
docker compose logs scheduler-worker --tail 200 | Select-String -Pattern "scheduler_worker.*Started|code_dispatch|dispatch.*loop" | Select-Object -Last 10

$elapsed = ((Get-Date) - $start).TotalSeconds
Write-Host ""
Write-Host "Done in $([Math]::Round($elapsed,1))s. Run .\scripts\dispatch-diagnose.ps1 for a full state snapshot."
