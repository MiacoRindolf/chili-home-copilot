# Manual queue: insert a plan_tasks row the dispatch miner can pick up (planner_coding / PlanTask).
# Table ``coding_tasks`` is not in this schema; tasks live in ``plan_tasks.coding_readiness_state``.
# After insert, creates plan_task_coding_profile bound to a CodeRepo reachable in scheduler-worker
# (name chili-home-copilot, path /app) so agent_suggest and sandbox apply/validate can run.
# Requires: at least one plan_projects row (uses the lowest project id).
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Description,
    [Parameter()][Nullable[int]]$RepoId = $null
)
$ErrorActionPreference = "Stop"
$env:PGPASSWORD = "chili"
function Invoke-Psql($Sql) {
    $raw = docker compose exec -T postgres psql -U chili -d chili -t -A -c $Sql 2>&1
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $raw" }
    return ($raw | ForEach-Object { "$_" }) -join "" | ForEach-Object { $_.Trim() }
}

# Discovery: no ``coding_tasks`` table in production schema — use plan_tasks
$checkCt = Invoke-Psql "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='coding_tasks');"
if ($checkCt -eq "t") {
    Write-Host "Unexpected: public.coding_tasks exists. Refusing to guess column layout. Use a migration or manual SQL." -ForegroundColor Red
    exit 1
}

$projectId = Invoke-Psql "SELECT id FROM plan_projects ORDER BY id ASC LIMIT 1;"
if ([string]::IsNullOrWhiteSpace($projectId)) {
    Write-Host "No plan_projects row. Create a project in the UI first (or insert plan_projects)." -ForegroundColor Red
    exit 1
}

# Escape single quotes for SQL string literals
function SqlEscape([string]$s) {
    return $s -replace "'", "''"
}
$t = SqlEscape $Title
$d = SqlEscape $Description

$sql = @"
INSERT INTO plan_tasks (project_id, title, description, status, priority, coding_readiness_state, coding_workflow_mode, coding_workflow_state, coding_workflow_state_updated_at)
VALUES (
  $projectId,
  '$t',
  '$d',
  'todo',
  '50',
  'ready_for_dispatch',
  'tracked',
  'unbound',
  NOW()
) RETURNING id;
"@

Write-Host "Inserting plan_tasks row in project $projectId ..."
$out = docker compose exec -T postgres psql -U chili -d chili -t -A -c $sql.Trim() 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "INSERT failed: $out" -ForegroundColor Red; exit 1 }
$taskId = ($out | ForEach-Object { "$_" } | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1)
if (-not $taskId) { $taskId = ($out -join "").Trim() }
Write-Host ('OK: plan_tasks.id=' + $taskId + ' coding_readiness_state=ready_for_dispatch') -ForegroundColor Green

# --- bind workspace: code_repos + plan_task_coding_profile (idempotent) ---
$repoRowId = $null
if ($null -ne $RepoId -and $RepoId -gt 0) {
    $repoRowId = "$RepoId"
    Write-Host "Using -RepoId override: $repoRowId"
} else {
    $repoRowId = Invoke-Psql @"
SELECT id::text FROM code_repos
WHERE active IS TRUE
  AND (
    LOWER(name) = LOWER('chili-home-copilot')
    OR path = '/app'
    OR container_path = '/app'
  )
ORDER BY id ASC
LIMIT 1;
"@
    if ([string]::IsNullOrWhiteSpace($repoRowId)) {
        Write-Host "No matching code_repos; inserting chili-home-copilot @ /app (scheduler image mount)..."
        $repoRowId = docker compose exec -T postgres psql -U chili -d chili -t -A -c @"
INSERT INTO code_repos (path, name, host_path, container_path, user_id, file_count, total_lines, reachable_in_web, reachable_in_scheduler, active, created_at)
VALUES ('/app', 'chili-home-copilot', NULL, '/app', NULL, 0, 0, true, true, true, NOW())
ON CONFLICT (path) DO UPDATE SET
  name = EXCLUDED.name,
  host_path = EXCLUDED.host_path,
  container_path = EXCLUDED.container_path,
  active = true,
  reachable_in_web = true,
  reachable_in_scheduler = true
RETURNING id;
"@
        if ($LASTEXITCODE -ne 0) {
            Write-Host "code_repos upsert failed: $repoRowId" -ForegroundColor Red
            exit 1
        }
        $repoRowId = ($repoRowId | ForEach-Object { "$_" } | ForEach-Object { $_.Trim() } | Select-Object -First 1)
    }
}

if ([string]::IsNullOrWhiteSpace($repoRowId)) {
    Write-Host "Could not resolve code_repo_id for workspace binding." -ForegroundColor Red
    exit 1
}

Write-Host "Binding plan_task_coding_profile: task_id=$taskId -> code_repo_id=$repoRowId"
$proSql = @"
INSERT INTO plan_task_coding_profile (task_id, repo_index, code_repo_id, sub_path, updated_at)
VALUES ($taskId, 0, $repoRowId, '', NOW())
ON CONFLICT (task_id) DO UPDATE SET
  code_repo_id = EXCLUDED.code_repo_id,
  repo_index = EXCLUDED.repo_index,
  sub_path = EXCLUDED.sub_path,
  updated_at = NOW();
"@
$prOut = docker compose exec -T postgres psql -U chili -d chili -c $proSql.Trim() 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "plan_task_coding_profile upsert failed: $prOut" -ForegroundColor Red; exit 1 }
Write-Host $prOut
$verify = docker compose exec -T postgres psql -U chili -d chili -c "SELECT * FROM plan_task_coding_profile WHERE task_id = $taskId;" 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "Profile verify failed: $verify" -ForegroundColor Red; exit 1 }
Write-Host "plan_task_coding_profile row:" -ForegroundColor Cyan
Write-Host $verify

# Miner orders by plan_tasks.sort_order ASC, then id ASC — older ready_for_dispatch rows
# would starve this one. Nudge: highest priority to this task only within the same project/queue.
$sortSql = @"
UPDATE plan_tasks SET sort_order = 1000
WHERE project_id = $projectId
  AND coding_readiness_state = 'ready_for_dispatch'
  AND id <> $taskId;
UPDATE plan_tasks SET sort_order = -1 WHERE id = $taskId;
"@
$sortOut = docker compose exec -T postgres psql -U chili -d chili -c $sortSql.Trim() 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host "sort_order nudge failed: $sortOut" -ForegroundColor Red; exit 1 }
Write-Host "Miner priority: this task is sort_order=-1; other ready_for_dispatch in project $projectId are 1000." -ForegroundColor Cyan
Write-Host $sortOut
exit 0
