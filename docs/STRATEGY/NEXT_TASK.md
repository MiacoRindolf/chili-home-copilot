# NEXT_TASK: f-hygiene-2

STATUS: DONE

## Goal

Three soak-safe observability hardening items, bundled into one task. Same shape as F-hygiene-1: small, additive, zero strategy impact, no calibration retuning. The F8a soak window keeps running; this lands during the wait.

After this task:

1. **`db_errors = 13` on decay_miner is diagnosed.** Either documented as a known-transient category (no fix needed), or fixed at the actual error site. We've been seeing this stable-but-nonzero number since F8a-fix's verification window. F8a-evaluation surfaced it. Investigate now while the context is fresh.
2. **Watchdog silence-as-health flips to positive confirmation.** Currently the watchdog only logs on death. Adding a one-line `decay_miner watchdog OK` per supervisor tick (60s) makes "alive" observable rather than inferred. F-hygiene-1 Open Question 4, F8a-evaluation Open Question 3.
3. **`pending_heap` trend is observable.** Currently we infer "oscillating, not growing" from a single point-in-time read. A small diagnostic script that rolls a grep across `docker compose logs` to extract the time series of `pending_heap=N` values over the last N hours makes oscillation provable. F8a-evaluation Open Question 4.

Up to 3 commits, all soak-safe.

## Why now

- F8a-evaluation surfaced three observability gaps that all have the same flavor: things we *infer* are healthy could be made *observable* with small, additive changes. Same investment as F-hygiene-1's watchdog + `last_error` self-clear.
- F8a-evaluation-rerun is gated on the 24h soak (re-run at 2026-05-03 17:00 UTC). This task does NOT extend that window — all three items are observability-only, no behavior changes.
- `db_errors=13` is the kind of stable-but-undiagnosed thing that quietly grows. Cheap to investigate now.

## Architectural commitments

- **Event-driven shape preserved.** No new polling loops, no new DB writes, no new LISTEN channels.
- **No new magic numbers.** Any constants introduced (e.g., the rolling-grep window) are explicit, named, documented, in a UX/diagnostic category — not strategy thresholds.
- **Idempotent, additive changes.** No deletions of behavior.
- **No miner / scanner / executor / gate changes** beyond the explicit edits below.
- **No migrations.** No schema work.
- **Up to 3 commits.** Subtask 1 may produce 0 commits if the investigation concludes "known-transient, no action."

## Scope

### Commit 1 (or zero): Diagnose `db_errors = 13` on decay_miner

**Investigation first, fix only if warranted.**

Run:

```bash
docker compose logs fast-data-worker --since 24h \
  | grep -iE "decay_miner.*ERROR|decay_miner.*[Ee]xception|psycopg2.*decay" \
  | head -50
```

What we want to know:
- Is it the same error repeating, or different errors?
- Is it a known-transient category (psycopg2 reconnect, brief lock contention) or something deeper (constraint violation, type mismatch, unhandled JSONB shape)?
- Does the count keep growing (ticking up by 1 every N minutes) or is it frozen at 13 from a single burst at startup?

**Decision branches:**

- **A. Known-transient category** (e.g., `OperationalError: server closed the connection unexpectedly` retried successfully). Document in the CC report's "Findings" section as "expected operational noise; not actionable." No code commit. Add the error category to a comment near the `db_errors` counter so the next investigator sees the previous diagnosis.

- **B. Real bug in the miner's DB write path** (constraint violation, type mismatch). One commit fixing the actual error site. The fix should be surgical — not a refactor of the miner's flush logic.

- **C. Frozen at 13 from a startup-time burst** (no further growth). Document and move on. No code commit. May be worth resetting the counter on next supervisor restart, but don't fold a counter-reset into this task — that's its own decision.

**Constraint:** Don't suppress the error. If it's real, fix it. If it's not real, document it. Hiding it (try/except: pass without logging) is the wrong move.

**Verification:**
- If A or C: the CC report includes the grep output (sanitized) and the diagnosis category.
- If B: re-run the grep after the fix; new errors should be 0 or substantively reduced.

### Commit 2: Positive-confirmation `[fast_path] decay_miner watchdog OK` log line

**File:** `app/services/trading/fast_path/supervisor.py` (the `_decay_miner_watchdog` coroutine added in F-hygiene-1 commit `000fbc0`)

**What:**

Currently the watchdog only logs on task death. Silence implies health, but operators inferring health from absence is fragile. Add a positive-confirmation log line on each tick:

```python
async def _decay_miner_watchdog(task: asyncio.Task, status_tracker: StatusTracker) -> None:
    """Surface decay_miner task health every WATCHDOG_INTERVAL_S."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        if task.done():
            # ... existing death-handling logic unchanged ...
            return
        # F-hygiene-2: positive confirmation. Logged at INFO so it ends up
        # in the same place as the supervisor metrics tick. Aligns with
        # the 60s WATCHDOG_INTERVAL_S so each metrics tick has one
        # corresponding watchdog OK line.
        logger.info("[fast_path] decay_miner watchdog: OK")
```

**Status surface:** None changed. The log line is for human / log-aggregator consumption, not for `fast_path_status`. Keep `fast_path_status.last_error` as the death-only signal.

**Verification:**
- After deploy, `docker compose logs fast-data-worker --since 5m | grep "watchdog: OK"` shows ~5 lines (one per minute).
- After deploy, the `db_errors` line cadence is unchanged (this isn't a DB write).

### Commit 3: `pending_heap` time-series diagnostic script

**File:** `scripts/dispatch-decay-heap-trend.ps1` (new)

**What:**

Rolling-grep across `docker compose logs fast-data-worker` to extract the time series of `pending_heap=N` from supervisor metrics lines. Outputs to `scripts/dispatch-decay-heap-trend-output.txt` in the dispatch-script convention.

**Note:** Diagnostic script lives outside the application — no app code changes.

```powershell
# Extract pending_heap time series from fast-data-worker logs over the last N hours.
# Outputs: timestamp, pending_heap, obs_scheduled, obs_finalized, db_errors
# So we can answer: is pending_heap oscillating, growing, or stable? Is db_errors
# accumulating linearly with time, or frozen?
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..
$out = "scripts/dispatch-decay-heap-trend-output.txt"
"# decay_miner trend $(Get-Date -Format o)" | Out-File $out -Encoding utf8

# Default 24h window; override via first arg.
$hours = if ($args.Count -gt 0) { $args[0] } else { "24" }
"---window: last ${hours}h---" | Add-Content $out

docker compose logs fast-data-worker --since "${hours}h" 2>&1 `
  | Select-String -Pattern "decay_miner alerts=" `
  | ForEach-Object {
      # Each metrics line looks like:
      #   2026-05-02 17:45:27 [INFO] ... decay_miner alerts=1051 ... pending_heap=1112 ... db_errors=13 ...
      $line = $_.ToString()
      if ($line -match "(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})") {
        $ts = $Matches[1]
        $alerts = if ($line -match "alerts=(\d+)")          { $Matches[1] } else { "?" }
        $heap   = if ($line -match "pending_heap=(\d+)")    { $Matches[1] } else { "?" }
        $sched  = if ($line -match "obs_scheduled=(\d+)")   { $Matches[1] } else { "?" }
        $final  = if ($line -match "obs_finalized=(\d+)")   { $Matches[1] } else { "?" }
        $errs   = if ($line -match "db_errors=(\d+)")       { $Matches[1] } else { "?" }
        "$ts  alerts=$alerts  heap=$heap  scheduled=$sched  finalized=$final  errs=$errs"
      }
    } `
  | Add-Content $out

Write-Output "done"
```

**Verification:** Run the script; output should show ~60 entries per hour (one per supervisor tick). If `pending_heap` oscillates between, say, 800 and 1200 over 24h with no monotonic trend, the heap is healthy. If it grows linearly, that's a finding.

**Note on a no-code alternative:** We could persist this to a small `fast_path_metrics_history` table for SQL queryability. Don't. The dispatch-script + log-grep approach is zero-code, perfectly soak-safe, and equally informative for the question we're asking. If we ever need to query metrics history programmatically (e.g., a brain consumer), THAT is when we add the table — not now.

## Brain integration (reuse, don't rewrite)

- `_decay_miner_watchdog` (commit 2) — extend the existing F-hygiene-1 coroutine in place. Single new log line, no structural change.
- Diagnostic script (commit 3) — uses the dispatch-script convention already established (e.g., `scripts/dispatch-fast-path-soak-status.ps1`).
- Investigation (commit 1) — pure log-reading, no app code unless a real bug surfaces.

## Constraints / do not touch

- **All 8 live-placement safety belts.** No changes.
- **Default mode stays paper.**
- **No strategy threshold tuning.** Don't touch `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, `MAX_PENDING_DEFERRED`, `MIN_SAMPLES`, the negative-edge exclusion criterion, or any gate.
- **No miner code changes** beyond fixing a real bug if subtask 1 finds one. The fix would be surgical to the actual error site — not a refactor of the flush logic.
- **No migrations.**
- **No new gates.**
- **No restart policy on decay_miner crash.** Out of scope (still — same as F-hygiene-1).
- **No new DB tables, no new columns.** The `pending_heap` time-series is log-grep, not persisted.
- **`models/trading.py`, `.env.example`, executor, exit_manager.** Continue to leave them alone.

## Out of scope

- Restart policy / supervised retry on decay_miner crash. Future task.
- Watchdogs on other asyncio tasks (scanner, ws_listener, exit_manager). Same pattern, but doing all of them in one go is a refactor.
- Persisted metrics-history table for programmatic querying. Premature.
- Resetting the `db_errors` counter at supervisor boot (separate decision; could be useful but folds counter semantics into this task).
- F8a-evaluation-rerun (separate, later task — at 2026-05-03 17:00 UTC).
- F8b / F9.
- Any tuning of any threshold.

## Success criteria

1. `git log --oneline -5` shows up to 3 new commits, pushed to origin. Each commit message clearly identifies its subtask.
2. `docker compose ps fast-data-worker` healthy after deploy.
3. After Commit 2 deploys: `docker compose logs fast-data-worker --since 5m | grep "watchdog: OK"` returns ~5 lines.
4. After Commit 3 lands: running `.\scripts\dispatch-decay-heap-trend.ps1 24` produces a time-series of `pending_heap` values across the last 24h.
5. Subtask 1's investigation produces either a fix (if a real bug) or a documented diagnosis (if a known-transient category). NOT a silenced error.
6. F8a soak continues uninterrupted — 5 pairs streaming, capture rate stays at 100%, no behavioral changes from any commit.
7. `docs/STRATEGY/CC_REPORTS/<date>_f-hygiene-2.md` written following PROTOCOL.md format. Include:
   - The grep output from subtask 1 (sanitized of any sensitive paths/tokens) and the diagnosis category.
   - Per-commit verification.
   - The diagnostic-script's first run output (the time series of `pending_heap` values).

## Open questions for Cowork (surface in your report only if relevant)

1. **If subtask 1 finds the error category is "psycopg2 reconnect retried successfully,"** the fix is "do nothing, document it." But if it finds something dirtier (e.g., a constraint violation that the miner is silently retrying without resolving), is the fix in scope? My instinct: yes, surgically, if the error site is obvious. If it requires a structural change to the miner's flush, that's out of scope and gets its own brief.

2. **The watchdog OK log adds ~5 lines/min to the supervisor log volume.** Insignificant compared to the ~50-100 lines/min the supervisor already emits. Worth flagging only if your log-aggregator has cost-per-line concerns.

3. **The dispatch script's default 24h window** matches the F8a soak cadence. If you want a different default (e.g., 6h to focus on intra-soak trends), say so.

4. **Subtask 1's investigation might find that the 13 errors are all from F-hygiene-1's deploy window** (network outage, container restart). If so, the count is frozen and benign — but might recur on the *next* network outage. Worth proactively adding a counter-reset on supervisor boot? Out of scope here, but flag if it surfaces as a real concern.

## Rollback plan

- Each commit is individually revertable. Commit 1 either has no code change (rollback is just removing the diagnosis comment) or has a surgical fix to one error site (revert restores the prior error). Commit 2's revert removes the OK log line. Commit 3's revert removes the diagnostic script — purely additive in `scripts/`.
- No migrations. No data migrations. No schema changes.
- No live-placement risk: none of these touch the executor, gates, or any path that interacts with the broker.
