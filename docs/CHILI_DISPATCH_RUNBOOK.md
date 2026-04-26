# CHILI Dispatch — Operator Runbook

Companion to `CHILI_DISPATCH_AUTONOMOUS_DEV_PLAN.md`. This document is the on-call playbook for when Dispatch is running.

## Quick reference

| Symptom | First action |
|---|---|
| Dispatch won't stop / making bad commits | Trip the code kill switch (see §1) |
| Same task failing repeatedly | Check `code_agent_runs` last 5 rows for that `task_id` (see §3) |
| Premium token spend spiking | Check budget cap and tier mix (see §4) |
| Distillation candidate keeps regressing | Inspect `distillation_runs` last 3 rows (see §6) |
| Diff merged into a frozen path | This should be impossible — file an incident immediately (see §7) |

## 1. Kill switch — code agent

**Activate (immediate halt):**

```powershell
conda run -n chili-env python -c "from app.services.code_dispatch.governance import activate_code_kill_switch; activate_code_kill_switch('manual')"
```

**Deactivate:**

```powershell
conda run -n chili-env python -c "from app.services.code_dispatch.governance import deactivate_code_kill_switch; deactivate_code_kill_switch()"
```

**Status:**

```powershell
conda run -n chili-env python -c "from app.services.code_dispatch.governance import get_code_kill_switch_status; print(get_code_kill_switch_status())"
```

The kill switch persists in `code_kill_switch_state` and is restored on every scheduler restart. It is a separate switch from the trading kill switch (`trading_risk_state`). Activating one does not activate the other.

Auto-trip conditions:
- 5 consecutive cycles with `decision='escalate'` or `decision='rollback'`.
- Validation timeout cluster (3 timeouts in the last 10 cycles).
- Frozen-scope breach attempt (an LLM tried to edit a `severity='block'` path).
- Budget hard cap exceeded.

## 2. Manual override — pin a task to a tier

```sql
UPDATE coding_tasks SET force_tier = 4 WHERE id = <task_id>;
```

Forces this task to use only tier-4 (premium) on the next dispatch. Use for tasks you know are architecturally hard.

To unpin: `UPDATE coding_tasks SET force_tier = NULL WHERE id = <task_id>;`

## 3. Investigate a stuck task

```sql
SELECT cycle_step, decision, escalation_reason, started_at
FROM code_agent_runs
WHERE task_id = <task_id>
ORDER BY started_at DESC
LIMIT 10;
```

Check the validation artifacts:

```sql
SELECT step_key, exit_code, LEFT(stdout, 500), LEFT(stderr, 500)
FROM coding_validation_artifacts a
JOIN code_agent_runs r ON r.validation_run_id = a.run_id
WHERE r.task_id = <task_id>
ORDER BY a.id DESC
LIMIT 20;
```

Check the LLM trace:

```sql
SELECT provider, model, tier, success, weak_response, failure_kind, LEFT(completion, 400)
FROM llm_call_log
WHERE cycle_id IN (SELECT id FROM code_agent_runs WHERE task_id = <task_id>)
ORDER BY id DESC
LIMIT 10;
```

If the same task has 3+ failures across all four tiers, it is likely a real architectural problem. Promote it to your queue manually and clear `force_tier`.

## 4. Budget cap and tier mix

Daily tier mix:

```sql
SELECT date_trunc('day', created_at) AS day,
       tier,
       COUNT(*) AS calls,
       SUM(cost_usd) AS spend
FROM llm_call_log
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

Healthy mix in steady state (post-distillation, after Phase D.5):
- tier-1: 60%+
- tier-2: 25–35%
- tier-3: 5–10%
- tier-4: < 2%

Spike investigation:
- If tier-3/4 spike: distillation may have regressed. Check `distillation_runs` for a recent rejection.
- If tier-1 share dropped: Ollama may be down. Check `docker compose ps ollama` and `curl http://localhost:11434/api/tags`.

Set caps:

```powershell
$env:CHILI_DISPATCH_DAILY_USD_CAP = '5.00'
$env:CHILI_DISPATCH_HOURLY_USD_CAP = '0.75'
```

## 5. Frozen-scope guard

Inspect glob list:

```sql
SELECT glob, severity, reason FROM frozen_scope_paths ORDER BY severity, glob;
```

Add a new glob (always with reason):

```sql
INSERT INTO frozen_scope_paths (glob, severity, reason)
VALUES ('app/services/whatever/**', 'block', 'reason here');
```

Severity levels:
- `block` — Dispatch will refuse to apply diffs touching these paths. Run is recorded with `decision='escalate'`.
- `review_required` — Dispatch can produce a diff but must open a PR; never auto-merges.
- `warn` — Dispatch proceeds but logs a warning to the audit row.

## 6. Distillation operations

**Inspect last 5 distillation runs:**

```sql
SELECT id, started_at, base_model, candidate_tag, train_rows, eval_rows,
       incumbent_pass, candidate_pass, decision, decision_reason
FROM distillation_runs
ORDER BY id DESC LIMIT 5;
```

**Force a rollback to a previous candidate:**

```powershell
ollama cp chili-coder:<previous-tag> chili-coder:current
```

Then update `registry.current_tier_1_model` to point to the rolled-back tag.

**Pause distillation:**

```powershell
$env:CHILI_DISPATCH_DISTILLATION_ENABLED = '0'
```

Restart the scheduler. The `code_learning_cycle` continues to run; only the every-6h `distillation_cycle` is paused.

**Re-curate the golden eval set:**

The 100-task eval set lives in `app/services/distillation/golden_tasks.json`. Edit by hand. Always include diverse difficulty and at least 10 cross-file tasks.

## 7. Incident: frozen path got modified

Should not happen. If it does:

1. **Trip both kill switches** — code dispatch *and* (out of caution) the trading kill switch.
2. `git log --since="1 day ago" --name-only --author="dispatch"` to find the offending commit(s).
3. `git revert <sha>` for each.
4. Audit the gap: how did the diff get past `frozen_scope.diff_touches_frozen_scope`? Add a regression test in `tests/test_frozen_scope_guard.py`.
5. Document in `docs/INCIDENTS/` and reset only after a code review of `frozen_scope.py`.

## 8. Day-zero startup

```powershell
# 1. Apply migration 171 (after reviewing migrations_proposed/171_chili_dispatch_tables.py)
.\scripts\verify-migration-ids.ps1
# Then move the file into app/migrations.py and add to MIGRATIONS list

# 2. Ensure Ollama has a base model pulled
docker compose up -d ollama
docker exec -it chili-ollama-1 ollama pull qwen2.5-coder:7b

# 3. Start in shadow mode
$env:CHILI_DISPATCH_ENABLED = '1'
$env:CHILI_DISPATCH_MODE = 'shadow'           # 'shadow' | 'sandboxed' | 'branch' | 'auto-merge'
$env:CHILI_LLM_LOCAL_FIRST = '0'              # flip to 1 only after llm_call_log shows ollama hits work
$env:CHILI_DISPATCH_DAILY_USD_CAP = '5.00'
docker compose up scheduler-worker
```

## 9. Health checks

The brain page at `/brain/project` will surface dispatch metrics once the artifact is wired. Until then:

```sql
-- last 24h
SELECT
  COUNT(*) FILTER (WHERE decision IN ('merge','proceed')) AS happy,
  COUNT(*) FILTER (WHERE decision = 'escalate') AS escalations,
  COUNT(*) FILTER (WHERE decision = 'veto')      AS vetoes,
  COUNT(*) FILTER (WHERE decision = 'rollback')  AS rollbacks
FROM code_agent_runs
WHERE started_at > NOW() - INTERVAL '24 hours';
```

If `happy / total < 0.6` for 3 consecutive days, escalate to human review and pause dispatch.

## 10. The "I just want to write code by hand for a bit" toggle

```powershell
$env:CHILI_DISPATCH_PAUSE = '1'
docker compose restart scheduler-worker
```

This sets a soft pause flag (separate from the kill switch). The cycle still runs but every step short-circuits to `decision='deferred_user_session'`. Unpause by clearing the env var.

## 11. Synthetic operator-queued tasks

Queue a `plan_tasks` row **and** a `plan_task_coding_profile` that binds to the in-container app tree (`/app`) so the dispatch miner can run agent-suggest → worktree apply → validation.

**Prerequisite — scheduler-worker must have dispatch env**

`docker-compose.yml` maps `CHILI_DISPATCH_ENABLED` / `CHILI_DISPATCH_MODE` (defaults `0` / `shadow`). For sandboxed runs, set in the **host** environment or `.env` before `docker compose up`:

- `CHILI_DISPATCH_ENABLED=1`
- `CHILI_DISPATCH_MODE=sandboxed`

Then recreate `scheduler-worker` and confirm logs contain `code_dispatch ... ENABLED ... mode=sandboxed`.

`dispatch-queue-task.ps1` also bumps your new task’s `plan_tasks.sort_order` to **-1** and others in the same project queue to **1000** so the miner (which uses `ORDER BY sort_order ASC, id ASC`) does not keep servicing older `ready_for_dispatch` rows that lack a profile.

**Invocation (example that binds to the default synthetic repo or creates `chili-home-copilot` @ `/app`):**

```powershell
.\scripts\dispatch-queue-task.ps1 `
  -Title "D.2.6 closure smoke" `
  -Description "In app/services/code_dispatch/scorer.py, add a numpy-style docstring to the function task_complexity_score. Do not change any return values or imports. Keep it under 10 lines."
```

**Override which `code_repos` row to bind (optional):**

```powershell
.\scripts\dispatch-queue-task.ps1 -Title "…" -Description "…" -RepoId 3
```

**Lifecycle (typical good run):**

1. `plan_tasks` — new row, `id = <task_id>`, `coding_readiness_state = 'ready_for_dispatch'`.
2. `plan_task_coding_profile` — one row, `task_id = <task_id>`, `code_repo_id` → the repo for `/app` (script prints the row after upsert).
3. `code_agent_runs` — at least one row for that `task_id` with `cycle_step` through `apply` / `validate`, `decision` in `('passed','validation_failed',…)`, non-empty `diff_summary->files`, and `validation_run_id` set when validation ran.
4. `llm_call_log` — at least one row with `tokens_in` / `tokens_out` from the draft step.

**Cleanup after you are done with a throwaway task:**

```sql
-- Replace <task_id> with the id printed by the script.
DELETE FROM plan_task_coding_profile WHERE task_id = <task_id>;
DELETE FROM plan_tasks WHERE id = <task_id>;
```

(If you also inserted test-only `coding_agent_suggestion` or validation rows, delete or archive those in the same session if your workflow created them; the two statements above are the minimum for a synthetic operator queue line.)
