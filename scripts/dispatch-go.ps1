# Fire-and-forget: rebuild chili image, force-recreate scheduler-worker,
# wait for it to settle, then capture full diagnostic snapshot.
#
# Usage: .\scripts\dispatch-go.ps1
# Then: send "ok" to Claude. Claude reads scripts/dispatch-diagnose-output.txt directly.

$bigStart = Get-Date
Write-Host "=== CHILI Dispatch: full restart + diagnose ===" -ForegroundColor Cyan
Write-Host ""

# 1. Build (slow step). ./scripts/ is NOT bind-mounted, so any edit to
#    scripts/scheduler_worker.py needs a fresh image.
Write-Host "[1/5] Building chili-app:local (slow - pulls scripts/ into image)..." -ForegroundColor Yellow
docker compose build chili 2>&1 | Select-String -Pattern "ERROR|Built|naming|exporting|writing image" | Select-Object -Last 8
Write-Host ""

# 2. Pre-flight: ensure code_repos.container_path points to /workspace for the
#    chili-home-copilot repo. The dispatch sandboxed runner needs a path that
#    contains a real .git/ directory, and /workspace is the new bind mount.
#    Idempotent — safe to re-run.
Write-Host "[2/5] Pre-flight: setting code_repos.container_path=/workspace..." -ForegroundColor Yellow
docker compose exec -T postgres psql -U chili -d chili -c `
  "UPDATE code_repos SET container_path='/workspace' WHERE name='chili-home-copilot';" 2>&1 |
  Select-String -Pattern "UPDATE|ERROR" | Select-Object -First 3
Write-Host ""

# 3. Hard down + up: --force-recreate is unreliable for picking up new volume
#    mounts. A clean `compose stop && rm` then `up -d` guarantees the volume
#    config is re-applied (including the new .:/workspace bind mount).
Write-Host "[3/5] Hard-restarting scheduler-worker (stop + rm + up)..." -ForegroundColor Yellow
docker compose stop scheduler-worker 2>&1 | Select-Object -Last 3
docker compose rm -f scheduler-worker 2>&1 | Select-Object -Last 3
docker compose up -d scheduler-worker
Write-Host ""

# 4. Wait for the dispatch wiring to finish booting + first cycle attempt.
#    70s covers: container start, scheduler kickoff, first 60-second tick.
Write-Host "[4/5] Waiting 70s for scheduler-worker to boot + first dispatch tick..." -ForegroundColor Yellow
$waited = 0
while ($waited -lt 70) {
    Start-Sleep -Seconds 5
    $waited += 5
    Write-Host -NoNewline "."
}
Write-Host ""
Write-Host ""

# 5. Run the full diagnostic. dispatch-diagnose.ps1 writes the snapshot to
#    scripts/dispatch-diagnose-output.txt for Claude to Read directly.
Write-Host "[5/5] Capturing diagnostic snapshot..." -ForegroundColor Yellow
& "$PSScriptRoot\dispatch-diagnose.ps1"
Write-Host ""

$elapsed = ((Get-Date) - $bigStart).TotalSeconds
Write-Host "=== Done in $([Math]::Round($elapsed,1))s ===" -ForegroundColor Green
Write-Host ""
Write-Host "Now send 'ok' to Claude. Claude will read scripts/dispatch-diagnose-output.txt." -ForegroundColor Cyan
