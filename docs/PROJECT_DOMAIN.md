# Project Domain

The project domain is the developer cockpit behind `/brain?domain=project`.

## Goals

- Make the project surface feel like a daily-driver coding cockpit instead of a loose collection of agent panels.
- Keep repo setup, planner handoff, suggestion generation, snapshot save, dry-run apply, real apply, and validation explicitly user-triggered.
- Share one canonical workspace binding across repo registration, planner coding tasks, search, apply, and validation.
- Fail closed when a planner task is not bound to an active repo.

## Page Structure

The project page is split into four panes:

- `Workspace`
- `Planner Handoff`
- `Agents`
- `Feed`

`Workspace` is the first paint and is backed by `GET /api/brain/project/bootstrap`.

## Backend Shape

- `app/routers/brain_project.py` owns the live project/code HTTP surface.
- `app/services/project_domain_service.py` builds the bootstrap payload used for first paint.
- `app/services/coding_task/workspaces.py` resolves the canonical repo binding for planner coding tasks.
- `app/routers/planner_coding.py` reads and writes the planner coding profile, including `code_repo_id`.

## Frontend Shape

- `app/templates/brain_project_domain.html` is now just the top-level project shell and include list.
- `app/templates/brain_project_workspace_header.html` contains the shared workspace/header shell.
- `app/templates/brain_project_pane_handoff.html`, `app/templates/brain_project_pane_workspace.html`, `app/templates/brain_project_pane_agents.html`, and `app/templates/brain_project_pane_feed.html` split the four cockpit panes into maintainable partials.
- `app/templates/brain_project_agent_panel_product_owner.html`, `app/templates/brain_project_agent_panel_project_manager.html`, and `app/templates/brain_project_agent_panel_architect.html` keep the default agent panels separate from the top-level pane wrapper.
- `app/static/components/brain-project-domain.css` owns the project-only cockpit styling that previously lived inline in `brain.html`.
- `app/static/components/brain-project-domain.js` owns the workspace-first bootstrap, repo management, indexing controls, code search, feed refresh, and dev-assistant client actions.
- `app/static/components/brain-project-agents.js` owns the project agent tabs, PO/PM/architect dashboards, generic agent panels, and explicit cycle actions.
- `app/static/components/brain-project-handoff.js` owns the planner handoff bridge, snapshot save/apply controls, validation bridge, and launch-param behavior.
- `app/templates/brain.html` now provides the shared Brain shell, cross-domain styling, global helpers, tiny template-provided planner hint globals, and the final page boot sequence.

## Workspace Binding

Planner coding tasks bind to repos through `plan_task_coding_profile.code_repo_id`.

Expected behavior:

- If the bound repo exists and is active, suggest/apply/validate may be enabled.
- If the bound repo is missing or stale, the UI shows the reason and disables mutation actions.
- Legacy `repo_index` is only a fallback path during migration.

## Safe Workflow

The intended operator flow is:

1. Register a repo.
2. Run indexing.
3. Load planner handoff explicitly.
4. Generate a suggestion explicitly.
5. Save a suggestion snapshot explicitly.
6. Dry-run the patch explicitly.
7. Apply the snapshot explicitly.
8. Run validation explicitly.

Read-only summary calls should not mutate state.

## Testing

Focused coverage for the project domain currently lives in:

- `tests/test_brain_page_domain.py`
- `tests/test_brain_http_domain.py`
- `tests/test_brain_project_bootstrap.py`
- `tests/test_brain_project_routes.py`
- `tests/test_brain_project_static_assets.py`
- `tests/test_planner_coding*.py`

`tests/test_brain_project_static_assets.py` parses the extracted project JavaScript assets and checks their exported hooks so the split frontend modules stay valid even when the shared page shell changes.

The project/coding test harness was also adjusted so these slices avoid full-schema truncate/reset behavior between cases.

## Operator Runbook

Short diagnosis + recovery steps for the common failure modes. Commands assume you are on the app host with the repo checked out and the venv active.

### Kill switch: disable the project domain without a redeploy

Set `PROJECT_DOMAIN_ENABLED=false` in the process environment (or `.env`) and restart the app. Effect:

- `GET /api/brain/project/bootstrap` returns **503** with `{"disabled": true, "domain": "project"}`.
- Every route on the `brain_project` router (`/api/brain/code/*`, `/api/brain/project/*`) and the `planner_coding` router (`/api/planner/tasks/{id}/coding/*`) returns **503**.
- `GET /brain?domain=project` forces a redirect to the domain hub, skips the pane render, and skips `brain-project-*.js`/`.css` asset downloads.
- `/api/brain/domains` omits the `"project"` entry so the hub grid does not render the tile.

Re-enable by flipping the flag back to `true` and restarting. There is no per-user override — it is global.

### Planner task bound to a stale or deleted repo

**Symptom:** Bootstrap returns `workspace_bound=false` with a `workspace_reason` string in `planner_handoff.summary.ops_hints`. Calls to `suggest`/`apply`/`validate` return **HTTP 409** with body `{"workspace_unbound": true, "workspace_reason": "..."}`.

**Cause:** `plan_task_coding_profile.code_repo_id` uses `ON DELETE SET NULL`. When a `CodeRepo` row is deleted or flipped to `active=false`, the profile keeps its row but loses its binding. The fail-closed path is triggered intentionally.

**Fix:** Rebind the task. Either:

- `POST /api/planner/tasks/{task_id}/coding/profile` with `{"code_repo_id": <id of an active repo>}`, or
- Restore the original repo with `POST /api/brain/code/repos` (or flip `active=true` in the DB), then refresh the bootstrap.

**Verify:** Re-fetch `/api/brain/project/bootstrap?planner_task_id={task_id}` and confirm `capabilities.suggest.enabled=true`.

### Apply failed mid-snapshot

**Symptom:** A real apply call (`POST /api/planner/tasks/{task_id}/coding/agent-suggestions/{suggestion_id}/apply` with `dry_run=false`) returned non-2xx after a successful dry-run, and the workspace may have partial writes.

**Diagnose:**

```bash
cd <bound workspace root>   # the path shown in handoff.profile.repo_path
git status                  # any staged/unstaged changes?
git diff                    # what was written?
```

Also check the audit trail:

```sql
SELECT id, suggestion_id, task_id, dry_run, status, message, ts
FROM coding_agent_suggestion_apply
WHERE suggestion_id = <suggestion_id>
ORDER BY id DESC;
```

The last row for that `suggestion_id` holds the failure message (`git apply failed after successful check: ...`).

**Fix:** Decide whether to keep the partial change or revert.

- **Revert** (most common, safe default):
  ```bash
  git checkout -- .
  git clean -fd   # only if you know no uncommitted work lives here
  ```
  Then re-run dry-run from the UI; if it passes, retry the real apply.

- **Keep** (only if the partial state happens to be correct): commit it manually, note the suggestion_id in the commit message, and skip the retry.

**Verify:** `git status` is clean; `GET /api/planner/tasks/{task_id}/coding/agent-suggestions/{suggestion_id}/apply-attempts` shows the audit trail.

### Validation run stuck in `validation_pending` / `running`

**Symptom:** A task has `coding_readiness_state = "validation_pending"` and the latest `coding_task_validation_run` has `status = "running"` for longer than the validator timeout.

**Cause:** Normally the service's `finally` guard coerces both to terminal states before commit. If the Python process was killed mid-commit (SIGKILL, power loss, OOM), PostgreSQL rolls back the open transaction — so in most cases there is nothing to repair. Only the case where an earlier successful flush + a separate failed commit could leave stale state, and this is what to run after an unclean shutdown.

**Repair:**

```sql
-- Reconcile stuck run rows (usually 0 after a clean restart)
UPDATE coding_task_validation_runs
SET status = 'failed',
    exit_code = COALESCE(exit_code, 1),
    error_message = COALESCE(error_message, 'reconciled after unclean shutdown'),
    finished_at = COALESCE(finished_at, NOW())
WHERE status = 'running'
  AND started_at < NOW() - INTERVAL '30 minutes';

-- Corresponding task state reconciliation
UPDATE plan_tasks
SET coding_readiness_state = 'blocked', updated_at = NOW()
WHERE coding_readiness_state = 'validation_pending';
```

**Verify:** No rows match `status = 'running' AND started_at < NOW() - INTERVAL '30 minutes'`; bootstrap shows `validate.enabled` per current profile state.

### Restoring from `pg_dump`

Backups from `scripts/backup_chili_db.ps1` live in `D:\CHILI-Docker\backup` with 14-day retention. To restore:

1. Stop the app process.
2. Identify the dump: `ls -lt D:/CHILI-Docker/backup | head`.
3. Restore into a fresh staging database first (never the live DB on the first try):
   ```bash
   pg_restore --clean --if-exists --dbname=postgresql://chili:chili@localhost:5433/chili_staging  <dump>
   ```
4. Point the app at `chili_staging`, verify the four-pane cockpit loads and a known task renders handoff state correctly.
5. If staging validates, repeat the restore against live. Keep the prior live dump — do not overwrite.

### Legacy `repo_index` — migration status

The `PlanTaskCodingProfile.repo_index` column is a transitional fallback from before canonical `code_repo_id` binding (migration 4270). It is still **read** by `workspaces.resolve_workspace_repo_from_legacy_index` and **written** by `workspaces.bind_profile_workspace` when a user binds by `repo_name`. The phased retirement is tracked in Phase 2 of the tech-debt plan:

1. Stop writing `repo_index` when `code_repo_id` is resolvable.
2. Monitor for one release — writes to the column should trend to zero.
3. Remove the reader branches and drop the column.

Until step 3 lands, do not assume `repo_index` is dead.
