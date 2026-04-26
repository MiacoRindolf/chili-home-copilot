# End-to-end reactive brain smoke test.
#
# Inserts a fresh plan_task with a unique title, queues it, watches the brain
# react, asserts what happened, cleans up. Output goes to
# scripts/brain-smoke-test-output.txt for Claude to Read.
#
# By default the budget cap is forced to $0 for the duration of the test, so
# the brain MUST route the new task to ESCALATE (no patterns, no local model,
# no budget). That proves the budget gate works WITHOUT spending money.
#
# Pass -SpendReal to leave the budget cap untouched and let the brain spend
# real money on the test task (routes to PREMIUM, calls gpt-5.1 via the
# existing dispatch sandbox flow). Use only when you want to verify the
# happy path end-to-end.
#
# Usage:
#   .\scripts\brain-smoke-test.ps1            # safe (no spend)
#   .\scripts\brain-smoke-test.ps1 -SpendReal # spends real money

param(
    [switch]$SpendReal
)

$out = "scripts/brain-smoke-test-output.txt"
$start = Get-Date
"# brain-smoke-test snapshot $start (SpendReal=$SpendReal)" | Out-File $out -Encoding utf8

function Append { param([string]$Line) $Line | Add-Content $out }
function Section { param([string]$Title) Append ""; Append "===== $Title =====" }
function PsqlQuery {
    param([string]$Sql)
    docker compose exec -T postgres psql -U chili -d chili -t -A -c $Sql 2>&1
}
function PsqlExec {
    param([string]$Sql)
    docker compose exec -T postgres psql -U chili -d chili -c $Sql 2>&1 | Out-Null
}

# -------- 0) Pre-flight --------
Section "Pre-flight"
$savedCap = (PsqlQuery "SELECT daily_premium_usd_cap FROM code_brain_runtime_state WHERE id=1;").Trim()
$savedMode = (PsqlQuery "SELECT mode FROM code_brain_runtime_state WHERE id=1;").Trim()
Append "saved cap=$savedCap mode=$savedMode"

if (-not $SpendReal) {
    Append "Forcing daily_premium_usd_cap=0 so the test cannot spend."
    PsqlExec "UPDATE code_brain_runtime_state SET daily_premium_usd_cap = 0 WHERE id = 1;"
} else {
    Append "SpendReal=true: leaving budget cap at $savedCap (real LLM call may happen)"
}

# -------- 1) Insert fresh test task --------
Section "Insert test task"
$tag = "brain-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + (Get-Random -Maximum 99999)
$title = "Brain smoke test $tag"
$desc  = "This is a synthetic smoke-test task created by scripts/brain-smoke-test.ps1. " +
         "It should be routed by the reactive Code Brain. Tag=$tag"
Append "tag=$tag"

# Find a project_id to attach to (any existing project works).
$projectId = (PsqlQuery "SELECT id FROM plan_projects ORDER BY id LIMIT 1;").Trim()
if (-not $projectId) {
    Append "ERROR: no plan_projects rows found; cannot create a test task. ABORT."
    if (-not $SpendReal) {
        PsqlExec "UPDATE code_brain_runtime_state SET daily_premium_usd_cap = $savedCap WHERE id = 1;"
    }
    return
}
Append "project_id=$projectId"

PsqlExec "INSERT INTO plan_tasks (project_id, title, description, status, coding_readiness_state) VALUES ($projectId, '$title', '$desc', 'todo', 'ready_for_dispatch') RETURNING id;"
$taskId = (PsqlQuery "SELECT id FROM plan_tasks WHERE title = '$title' ORDER BY id DESC LIMIT 1;").Trim()
if (-not $taskId) {
    Append "ERROR: insert appeared to succeed but task not found. ABORT."
    if (-not $SpendReal) {
        PsqlExec "UPDATE code_brain_runtime_state SET daily_premium_usd_cap = $savedCap WHERE id = 1;"
    }
    return
}
Append "test task_id=$taskId title='$title'"

# -------- 2) Wait for the brain to react --------
Section "Wait for routing decision"
$decisionId = ""
$decisionRow = ""
$elapsed = 0
$maxWait = 120
while ($elapsed -lt $maxWait) {
    Start-Sleep -Seconds 5
    $elapsed += 5
    $decisionRow = (PsqlQuery "SELECT id || '|' || decision || '|' || COALESCE(outcome,'') || '|' || COALESCE(cost_usd::text,'0') FROM code_decision_router_log WHERE task_id = $taskId ORDER BY id DESC LIMIT 1;").Trim()
    if ($decisionRow) {
        $decisionId = ($decisionRow -split '\|')[0]
        Append "decision row appeared after $elapsed s: $decisionRow"
        break
    }
    Append "  ... still waiting ($elapsed s, queue/decision empty)"
}

if (-not $decisionRow) {
    Append "FAIL: no routing decision row appeared in $maxWait s"
} else {
    $parts = $decisionRow -split '\|'
    $decision = $parts[1]
    $outcome = $parts[2]
    $cost = $parts[3]
    Append ""
    Append "decision=$decision  outcome=$outcome  cost_usd=$cost"
    if (-not $SpendReal) {
        if ($decision -eq "escalate" -and ($cost -eq "" -or $cost -eq "0" -or [decimal]$cost -le 0)) {
            Append "PASS: brain escalated the task without spending money (budget cap = 0)."
        } else {
            Append "FAIL: expected decision=escalate cost_usd<=0, got decision=$decision cost_usd=$cost"
        }
    } else {
        if ($decision -eq "premium") {
            Append "PASS: brain routed to PREMIUM (real LLM call). Watch budget."
        } elseif ($decision -eq "escalate") {
            Append "OK: brain escalated (likely budget exhausted or strikeout)."
        } else {
            Append "INFO: decision=$decision (template/local/skip — surprising for a fresh task)"
        }
    }
}

# -------- 3) Show full router-log row + scheduler-worker log slice --------
Section "Full code_decision_router_log row"
docker compose exec -T postgres psql -U chili -d chili -c "SELECT * FROM code_decision_router_log WHERE task_id = $taskId ORDER BY id DESC LIMIT 1;" 2>&1 | Add-Content $out

Section "Full code_brain_events row"
docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, event_type, subject_kind, subject_id, claimed_at, processed_at, outcome, error_message FROM code_brain_events WHERE subject_kind = 'plan_task' AND subject_id = $taskId ORDER BY id DESC LIMIT 5;" 2>&1 | Add-Content $out

Section "scheduler-worker log lines mentioning this task"
docker compose logs scheduler-worker --tail 1000 2>&1 |
    Select-String -Pattern "task_id=$taskId|subject=plan_task/$taskId|task $taskId" |
    Select-Object -Last 30 |
    ForEach-Object { Append $_.ToString() }

# -------- 4) Cleanup --------
Section "Cleanup"
PsqlExec "DELETE FROM code_decision_router_log WHERE task_id = $taskId;"
PsqlExec "DELETE FROM code_brain_events WHERE subject_kind = 'plan_task' AND subject_id = $taskId;"
PsqlExec "DELETE FROM plan_tasks WHERE id = $taskId;"
Append "cleaned up task $taskId + its events/decisions"

if (-not $SpendReal) {
    PsqlExec "UPDATE code_brain_runtime_state SET daily_premium_usd_cap = $savedCap WHERE id = 1;"
    Append "restored daily_premium_usd_cap to $savedCap"
}

# -------- 5) Final state --------
Section "Final runtime state"
docker compose exec -T postgres psql -U chili -d chili -c "SELECT mode, daily_premium_usd_cap AS cap, spent_today_usd AS spent FROM code_brain_runtime_state;" 2>&1 | Add-Content $out

$totalSec = ((Get-Date) - $start).TotalSeconds
Append ""
Append "===== Done in $([Math]::Round($totalSec,1))s ====="

Write-Host ""
Write-Host "Smoke test complete. Output: $out" -ForegroundColor Green
Write-Host "Send 'ok' so Claude can read it."
