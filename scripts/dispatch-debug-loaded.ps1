# Hard-restart scheduler-worker, wait, then capture everything Claude needs
# to diagnose the InFailedSqlTransaction issue:
#   1) what trigger_watcher.py looks like ON DISK
#   2) what trigger_watcher.py looks like AS LOADED into the running Python
#   3) raw scheduler-worker logs from AFTER the fresh start
#
# Output goes to scripts/dispatch-debug-loaded-output.txt for Claude to Read.
# Usage: .\scripts\dispatch-debug-loaded.ps1

$out = "scripts/dispatch-debug-loaded-output.txt"
$start = Get-Date
"# dispatch-debug-loaded snapshot $start" | Out-File $out -Encoding utf8

function Write-Section {
    param([string]$Title, [scriptblock]$Body)
    "" | Add-Content $out
    "===== $Title =====" | Add-Content $out
    try {
        $result = & $Body 2>&1
        if ($result) { $result | Out-String | Add-Content $out }
    } catch {
        "ERROR running section: $_" | Add-Content $out
    }
}

Write-Host "[1/4] Hard down + up scheduler-worker..." -ForegroundColor Yellow
docker compose down scheduler-worker 2>&1 | Select-Object -Last 3 | Out-Null
docker compose up -d scheduler-worker 2>&1 | Select-Object -Last 3 | Out-Null

Write-Host "[2/4] Waiting 90s for scheduler-worker to boot + a couple of 30s ticks..." -ForegroundColor Yellow
$waited = 0
while ($waited -lt 90) {
    Start-Sleep -Seconds 5
    $waited += 5
    Write-Host -NoNewline "."
}
Write-Host ""

Write-Host "[3/4] Capturing diagnostic..." -ForegroundColor Yellow

Write-Section "Container status" {
    docker compose ps scheduler-worker
}

Write-Section "trigger_watcher.py ON DISK (lines 100-115 + 165-200)" {
    "--- lines 100-115 (existence check) ---"
    docker compose exec -T scheduler-worker sed -n '100,115p' /app/app/services/code_brain/trigger_watcher.py
    ""
    "--- lines 165-200 (rollback + return + queue_depth) ---"
    docker compose exec -T scheduler-worker sed -n '165,200p' /app/app/services/code_brain/trigger_watcher.py
}

Write-Section "trigger_watcher.py AS LOADED by Python" {
    docker compose exec -T scheduler-worker python -c @"
import app.services.code_brain.trigger_watcher as m, inspect
src = inspect.getsource(m).splitlines()
print('FILE:', m.__file__)
print('total_lines:', len(src))
print('LINE_171:', repr(src[170]) if len(src) > 170 else 'N/A')
print('LINE_178:', repr(src[177]) if len(src) > 177 else 'N/A')
print('LINE_190:', repr(src[189]) if len(src) > 189 else 'N/A')
print('has_rollback:', 'db.rollback()' in inspect.getsource(m))
print('has_existence_check:', 'coding_task_validation_run' in inspect.getsource(m) and 'information_schema.tables' in inspect.getsource(m))
"@ 2>&1
}

Write-Section "Last 60 log lines (post-restart)" {
    docker compose logs scheduler-worker --tail 60 2>&1
}

Write-Section "code_brain log lines only" {
    docker compose logs scheduler-worker --tail 500 2>&1 |
        Select-String -Pattern "code_brain|REACTIVE|LEGACY|run_all_watchers|process_one_event" |
        Select-Object -Last 30
}

Write-Section "Tracebacks AFTER latest restart" {
    $lines = @(docker compose logs scheduler-worker --tail 1000 2>&1 | ForEach-Object { $_.ToString() })
    $lastStart = -1
    for ($i = $lines.Count - 1; $i -ge 0; $i--) {
        if ($lines[$i] -match "scheduler_worker.*Started") { $lastStart = $i; break }
    }
    if ($lastStart -lt 0) {
        "(no Started line found in last 1000 lines)"
    } else {
        "Latest Started at log line $lastStart"
        ""
        $tbIndices = @()
        for ($i = $lastStart; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match "Traceback \(most recent call last\)") { $tbIndices += $i }
        }
        if ($tbIndices.Count -eq 0) {
            "(no tracebacks AFTER latest Started - clean!)"
        } else {
            foreach ($idx in $tbIndices) {
                $end = [Math]::Min($idx + 25, $lines.Count - 1)
                "----- Traceback at log line $idx -----"
                for ($j = $idx; $j -le $end; $j++) { $lines[$j] }
                ""
            }
        }
    }
}

Write-Section "code_brain DB state" {
    "--- runtime_state ---"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, daily_premium_usd_cap, spent_today_usd, last_pattern_mining_at FROM code_brain_runtime_state;"
    ""
    "--- queue depth ---"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT COUNT(*) AS unclaimed FROM code_brain_events WHERE claimed_at IS NULL;"
    ""
    "--- recent decisions ---"
    docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, decision, task_id, outcome FROM code_decision_router_log ORDER BY id DESC LIMIT 5;"
}

$elapsed = ((Get-Date) - $start).TotalSeconds
"" | Add-Content $out
"===== Done in $([Math]::Round($elapsed,1))s =====" | Add-Content $out

Write-Host "[4/4] Done - written to $out ($([Math]::Round($elapsed,1))s)" -ForegroundColor Green
