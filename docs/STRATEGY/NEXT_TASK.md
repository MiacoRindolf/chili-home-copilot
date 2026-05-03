# NEXT_TASK: f-leak-1

STATUS: DONE

## Goal

Diagnose and contain the recurring host RAM-pressure event that prevents the operator from using their computer (can't open apps, browser unresponsive) approximately every 12h around 6 AM / 6 PM PT. Cross-cutting concern: the chili container has restarted 7 times in 16h with status "unhealthy," which means writes may be partially completing — potential data corruption risk.

This is **a real operational urgency, not a hygiene pass.** The user has explicitly elevated it above F9 (next strategic work). Subtasks are ordered for maximum protection-first.

After this task:

1. **Host is protected from container memory growth** even before the underlying leaks are fixed (defense-in-depth via container limits + WSL2 cap).
2. **The leak source is identified with evidence**, not inferred from a single snapshot. Continuous instrumentation captures the next 12h of memory growth.
3. **Postgres integrity is verified** for tables written by the restart-prone `chili` container.
4. **At minimum one of the two leaky containers (`chili` or `brain-worker`) has its leak fixed surgically.**

Up to 5 commits across 5 subtasks. Some are config (no app code), some are pure observability, one is the actual fix.

## Why now

Direct evidence from `scripts/dispatch-host-leak-probe-output.txt` (run 2026-05-03 04:44 UTC):

| Container | Mem usage / limit | Status | Restarts | CPU |
|---|---|---|---|---|
| **chili-home-copilot-chili-1** | **2.998 GiB / 3 GiB (99.94%)** | **unhealthy** | **7** | 1.42% |
| **chili-home-copilot-brain-worker-1** | 5.83 GiB / 8 GiB (72.88%) | healthy | 0 | **109.88%** |
| chili-home-copilot-scheduler-worker-1 | 3.32 GiB / 10 GiB (33%) | healthy | 0 | 38.95% |
| chili-home-copilot-broker-sync-worker-1 | 194 MiB / 4 GiB (4.7%) | healthy | 0 | 0% |
| chili-home-copilot-autotrader-worker-1 | 273 MiB / 3 GiB (8.9%) | healthy | 0 | 0% |
| chili-home-copilot-fast-data-worker-1 | **157 MiB / 512 MiB (30.7%)** | healthy | 0 | 4.4% |
| chili-home-copilot-postgres-1 | 328 MiB / 2 GiB (16%) | healthy | 0 | 3% |
| chili-home-copilot-ollama-1 | 39 MiB / 6 GiB (0.6%) | healthy | 0 | 0% |

Total committed container memory: **~13.1 GiB** before WSL2/Docker overhead. On a 16 or 32 GiB host this is severe.

**fast-data-worker is clean** (157 MiB / 512 MiB) — confirms the F8a soak data is uncorrupted by the host instability. The leak source is upstream of the fast-path subsystem.

The 12h cycle the operator observed correlates with: **chili container OOM-cycling** (7 restarts in 16h ≈ one every ~2.3h, close to the 99.94% memory cap pattern). Plus brain-worker at CPU 109% adding sustained host pressure.

## Architectural commitments

- **Defense-in-depth.** Subtask 1 (instrumentation) + Subtask 2 (WSL2 cap) protect the host BEFORE we know exactly what's leaking. If the leak hunt takes longer than this single session, the host is still safe.
- **Continuous observability over snapshot diagnosis.** Add a docker-stats logger that runs every 60s and writes to a file the dispatch script can grep. One snapshot won't catch a leak; a 12-hour time series will.
- **Postgres integrity is non-negotiable.** Chili has restarted 7 times. Any write-heavy tables it touches need a consistency check. Repair if needed; document if clean.
- **Surgical fixes only.** No refactor of chili's request handling or brain-worker's mining pipeline. Find the cache/dict that's growing unboundedly and add an eviction policy or LRU cap. Same shape as FIX 50 (`_ind_cache` pruning, scheduler dropped 5 GiB → 1.5 GiB).
- **No live-placement or strategy-threshold changes.** This is pure infrastructure.

## Scope

### Subtask 1: Continuous docker-stats logger (instrumentation)

**Files:** `scripts/dispatch-stats-logger.ps1` (new — long-running daemon-style script), `scripts/dispatch-stats-trend.ps1` (new — reader/aggregator).

**What:**

The dispatch-host-leak-probe captured a snapshot. To prove a leak we need a time series. Add a small PowerShell script that:
- Runs in a side window (or via Task Scheduler), forever.
- Every 60s, runs `docker stats --no-stream --format ...` and appends one line per container to `scripts/_stats_log/YYYY-MM-DD.txt` (rolled daily).
- Each line: `<UTC timestamp> <container> mem=<MiB> cpu=<pct> netio=<bytes> blockio=<bytes> pids=<n>`.
- A second script (`dispatch-stats-trend.ps1 <hours>`) reads the rolling log and outputs per-container deltas + slopes for the last N hours.

This is the same pattern as `dispatch-decay-heap-trend.ps1` — log-roll + grep-aggregator. **No application code changes.** Just two scripts.

**Verification:**
- After Subtask 1 lands, operator runs `dispatch-stats-logger.ps1 &` (or as a Windows scheduled task) in a side window.
- After ~30 minutes, `dispatch-stats-trend.ps1 0.5` shows ~30 lines per container with memory deltas.

### Subtask 2: WSL2 memory cap (host-level defense)

**File:** `%USERPROFILE%\.wslconfig` (new or edited — host-side config, not in repo).

**What:**

WSL2's `vmmem` process can grow to consume up to 50% of host RAM by default — that's why container memory limits don't fully protect the host. Add a `.wslconfig` cap so vmmem can't starve the rest of the OS:

```ini
[wsl2]
memory=10GB         ; cap total WSL2 RAM (adjust based on host RAM; should be << host total)
processors=6        ; cap CPU cores WSL2 can use
swap=4GB            ; allow swap so OOM is less abrupt
swapFile=C:\\Users\\rindo\\.wsl-swap.vhdx
```

Then `wsl --shutdown` and restart Docker Desktop to apply.

**This is the single most-impactful change for the operator's "can't open apps" symptom.** Even if chili keeps OOM-cycling internally, the host will have RAM headroom for the browser and other apps.

**Constraint:** The exact `memory=` value depends on the operator's host RAM total (not visible from the sandbox). Brief assumes 16 or 32 GiB host. Claude Code should:
- Probe the host RAM (`wmic ComputerSystem get TotalPhysicalMemory` or `Get-CimInstance Win32_ComputerSystem | Select TotalPhysicalMemory`).
- Set `memory=` to ~50% of total RAM (operator can tighten later).
- Document the choice in the CC report.

**No application code changes.** This is a host config file change committed to a `docs/RUNBOOKS/` reference path so future sessions can find it.

**Verification:** After applying, `wsl --shutdown` then `docker stats` shows no container exceeding the cap. `Get-Counter '\Memory\Available MBytes'` shows materially more host memory available.

### Subtask 3: chili main app memory leak hunt (the actual fix)

**File(s):** TBD — depends on what the leak hunt finds. Most likely candidates based on prior leaks (`_ind_cache` in scheduler, FIX 50): unbounded dicts/lists in cached endpoints, response cache without TTL, predictions dict growing per-request, websocket connection lists.

**What:**

The chili container hit 2.998 GiB / 3 GiB (99.94%) and is restart-cycling. **Find the unbounded structure.**

Steps:
1. Restart chili container fresh (`docker compose restart chili`); record initial RSS.
2. Wait 30 minutes; record RSS again. Compute growth rate (MiB/min).
3. While container is fresh and growing, run `docker exec chili-home-copilot-chili-1 python -c "import gc, sys; objs = gc.get_objects(); from collections import Counter; print(Counter(type(o).__name__ for o in objs).most_common(20))"` to see object-count breakdown by type.
4. Look for types with anomalously high counts (dict, list, str). For dicts/lists, find the holders: `import gc; for o in gc.get_objects(): if isinstance(o, dict) and len(o) > 10000: print(id(o), len(o), type(o.__class__).__name__)`.
5. Identify the holding code path. Apply surgical eviction or LRU.

**Same pattern as FIX 50** (saved memory: scheduler `_ind_cache` unbounded growth → bounded with TTL/LRU, scheduler dropped 8.9 GiB → 1 GiB). The right fix here is structurally identical: find the cache, bound it.

**Decision branches:**
- **A. Identifiable single cache.** Surgical fix, one commit.
- **B. Multiple smaller leaks compounding.** Fix the largest one first; document the others for follow-up.
- **C. Can't identify in single session.** Document findings, ship Subtask 1 + 2 as protection, defer the actual fix to f-leak-2.

**Constraint:** Don't add a sweeping cache cap (like "all dicts limited to N entries") — that breaks correctness. Find the specific cache, fix the specific cache.

**Verification:** Restart chili, run for 30 min, observe RSS growth slope is ≤ 1/10th of pre-fix slope. The dispatch-stats-logger from Subtask 1 captures this.

### Subtask 4: brain-worker CPU 109% investigation

**File(s):** TBD — likely in `scripts/brain_worker.py` or `app/services/learning.py`.

**What:**

brain-worker is at sustained 109% CPU (i.e., one core fully pegged plus some on a second). 5.83 GiB memory is high but not maxed. The CPU pegging is the actively-host-stressing part — it's what makes the Windows scheduler unable to give CPU time to the operator's foreground apps.

Steps:
1. Check what brain-worker is currently doing: `docker exec chili-home-copilot-brain-worker-1 py-spy dump --pid 1` (if py-spy installed) or `docker exec ... ps auxf` to see process tree.
2. Check supervisor logs for the last hour: `docker compose logs brain-worker --since 1h | tail -100`.
3. Specifically look for: long FractionalBacktest run (saved memory note: 13h backtest), tight while-loop with no sleep, missing yield in async code.

**Decision branches:**
- **A. Long-running known job** (e.g., the 13h backtest from saved memory). Document; not a leak. May want to deprioritize via `nice` / `cgroup cpu shares` so it doesn't compete with foreground apps.
- **B. Runaway loop / regression.** Identify and fix.
- **C. Healthy CPU usage (intentional, bounded).** Document; possibly add a cgroup cpu cap in docker-compose so it can't peg cores indefinitely.

**Verification:** After action, `docker stats` shows brain-worker CPU back to a reasonable steady state (target: <80% sustained on a 6-core cap).

### Subtask 5: Postgres integrity check (data corruption risk)

**File:** `scripts/dispatch-postgres-integrity.ps1` (new).

**What:**

Chili has restarted 7 times. Each restart could have happened mid-write. Postgres' transactional guarantees mean rows themselves should be atomic, BUT:
- Multi-row writes that aren't wrapped in a single transaction can be partially-completed.
- Application-level invariants (FK consistency that's not enforced by the schema) can break.
- Lock leaks from connections that weren't cleanly closed.

Probe these specifically:
- **Orphaned rows in critical tables**: `fast_alerts` without matching `fast_executions` for the executions side; `fast_executions` without matching `fast_alerts`; `fast_exits` without matching `fast_executions`.
- **`pg_locks`** for long-held locks on critical tables.
- **`pg_stat_activity`** for any "idle in transaction" beyond a reasonable threshold.
- **Counts vs prior known-good snapshots** for the high-write tables (compare to numbers from the f8a-evaluation-rerun report: 191 post-fix `fast_alerts`, 142 closed pullback round trips, etc.).

**Decision branches:**
- **A. Clean.** Document in CC report. No commits beyond the diagnostic script.
- **B. Orphaned rows / broken FK.** Document scope. Repair via migration if surgical; flag for separate task if larger.
- **C. Lock leaks.** Document holders; recommend connection-pool tightening (separate task if needed).

**No data mutations in this subtask.** Read-only probe; repair (if needed) is its own subsequent commit.

## Brain integration (reuse, don't rewrite)

- Pattern: same as F-hygiene-1, F-hygiene-2 — observability via dispatch scripts in `scripts/`.
- Saved memory: **FIX 50** (`_ind_cache` unbounded growth, scheduler 5 GiB → 1.5 GiB) is the structural template for chili's likely fix.
- `dispatch-decay-heap-trend.ps1` is the precedent for the log-roll + grep-aggregator pattern in Subtask 1.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Untouched.
- **Default mode stays paper.**
- **No strategy threshold tuning.**
- **No miner / scanner / executor / gate code changes.**
- **No migrations** in this task. If Subtask 5 finds orphaned rows that need repair, that's a separate brief.
- **No fast-data-worker changes.** It's clean (157 MiB / 512 MiB) and stable.
- **Don't lower brain-worker's memory limit below 8 GiB** without understanding what it's doing (subtask 4 first).
- **Don't drop the chili memory limit** below current 3 GiB — that just makes restart-cycling faster. The fix is finding the leak.

## Out of scope

- Refactor of chili's request-handling layer.
- Refactor of brain-worker's mining pipeline.
- Migration to fix orphaned rows (if found in Subtask 5) — separate brief.
- F9 (new signal types) — pre-empted by this task; resumes once leak is contained.
- F-hygiene-3 (validation-count UPSERT, the optimization issues from f8a-evaluation-rerun's Open Questions) — defer.
- Long-term: moving to a leaner runtime, container redistribution, etc.

## Success criteria

1. `git log --oneline -10` shows ≥3 new commits (subtasks 1, 2, 5 minimum), pushed to origin. Subtasks 3 and 4 may produce 0 or 1 commit each depending on findings.
2. `scripts/dispatch-stats-logger.ps1` exists and is documented; operator confirms it can be left running.
3. `.wslconfig` is set with appropriate `memory=` cap based on host RAM probe.
4. `docs/STRATEGY/CC_REPORTS/<date>_f-leak-1.md` written with:
   - Per-subtask findings (especially the leak hunt's diagnosis).
   - The first ~1h of stats-logger output as evidence the instrumentation works.
   - Postgres integrity result (clean / dirty + scope).
   - Operator runbook lines: how to start the stats-logger, how to read the trend.
5. After Subtask 2 + restart: `Get-Counter '\Memory\Available MBytes'` shows materially more host memory.
6. After Subtask 3 (if a fix lands): chili RSS growth slope is ≤ 1/10th of pre-fix.
7. F8a soak continues uninterrupted on fast-data-worker. No behavioral changes to any strategy code path.

## Open questions for Cowork

1. **If Subtask 3 (chili leak hunt) can't identify the leak in one session,** should it ship Subtask 1 + 2 + 5 anyway and defer the chili fix to f-leak-2? My read: yes. Protection-first matters more than a complete fix in one go. The instrumentation will catch the next 12h of growth and make the next session diagnostic.

2. **WSL2 memory cap value.** The brief says 50% of host RAM. If the operator wants a different ratio (more aggressive cap = more headroom for foreground apps; less cap = more for chili), surface the choice. 50% is a defensible default.

3. **brain-worker CPU 109% — known long-running job or actual regression?** Saved memory mentions a 13h FractionalBacktest. If that's still running from a prior session, document and let it finish; don't kill mid-run. If it's a regression, fix.

4. **If Postgres integrity check finds orphaned rows referencing the 7 chili restarts**, is repair urgent or can it wait for a separate brief? My read: it depends on which tables. `fast_*` tables are critical (live trading lineage); `chat_*` or planner tables are less critical. Surface scope.

## Rollback plan

- Subtask 1 (stats-logger script): purely additive, revert removes the scripts.
- Subtask 2 (`.wslconfig`): host-side config; remove the file or set `memory=` higher and restart WSL.
- Subtask 3 (leak fix): one targeted file change; revert restores the prior cache behavior. Container memory will start growing again, but Subtask 2's WSL2 cap protects the host.
- Subtask 4 (brain-worker): if a code change, revert the file. If a docker-compose cgroup change, revert the compose file.
- Subtask 5 (integrity script): purely additive.

No live-placement risk: none of these touch the executor, gates, broker code, or strategy thresholds.
