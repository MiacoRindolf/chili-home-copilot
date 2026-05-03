# NEXT_TASK: f-leak-2

STATUS: DONE

## Goal

Identify and surgically fix the chili main app's memory leak. f-leak-1 contained the host-level RAM pressure (WSL2 cap + container limits + observability) but did not fix the underlying leak — chili still trends toward its 3 GiB cap and OOM-restart-cycles when triggered. This task identifies the specific leak holder using in-process diagnostics, then applies a surgical fix.

After this task:

1. **chili main app's leak source is identified** with a specific code-path and a specific Python type / closure that's surviving GC.
2. **A surgical fix lands** that either (a) bounds the offending structure (LRU/TTL/cap), (b) hoists a per-request constructor to module/app scope so it's reused, or (c) cancels/joins a Thread/Timer holder. Same surgical-fix shape as FIX 50 (`_ind_cache`).
3. **Verification shows ≤ 1/10th the prior memory growth slope** over a 30-minute observation window after the fix lands.
4. **The mem_watcher instrumentation stays in chili** so future leaks surface immediately, the same way scheduler-worker's mem_watcher already catches them.

Up to 3 commits: instrumentation, fix, optional follow-on.

## Why now

f-leak-1's findings (commits `b8e710d`, `ffcb0d9`, `ae2e7f4`) plus its CC report:

- **Host is now defensible** (WSL2 cap pending operator's `wsl --shutdown` + Docker restart). The leak hunt no longer races against the operator's ability to use their computer.
- **The dispatch-stats-logger from f-leak-1.1** has been running and now has a real time series for the chili RSS curve.
- **Scheduler-worker's existing mem_watcher (FIX 49)** is already showing the codebase's leak pattern: `_make_invoke_excepthook.<locals>.invoke_excepthook` survivors growing +14/min — that's `threading.py`'s per-Thread closure. **Something is creating Thread (or Timer) objects faster than GC reclaims them.** Same pattern almost certainly afflicts chili.
- **Operator action items from f-leak-1 are independent of this task.** Operator can apply WSL2 cap and start the stats-logger in parallel with this task running.

## Architectural commitments

- **Surgical fix, not refactor.** Find the specific holder; bound or hoist it. No restructuring of request handlers, no new abstraction layers.
- **Reuse, don't reinvent.** Lift mem_watcher from `scripts/brain_worker.py` (or wherever FIX 49 placed it) into chili as-is. Don't write a new diagnostic.
- **Event-driven shape preserved.** No new polling loops, no new background tasks beyond what mem_watcher already does in scheduler-worker.
- **No migrations. No strategy threshold changes. No gate changes. No live-placement changes.**
- **Default mode stays paper. fast-data-worker untouched** (it's clean per f-leak-1's snapshot — 157 MiB / 512 MiB).

## Scope

### Commit 1: Lift `mem_watcher` from scheduler-worker into chili

**Files:** TBD via grep — likely `app/main.py` or wherever chili's startup lives, plus copy/import the existing mem_watcher helper.

**What:**

f-leak-1's CC report identified scheduler-worker's `mem_watcher` (FIX 49) as the right diagnostic surface — it logs RSS + top object-type counts every minute. chili doesn't have it. Lifting it gives chili the same in-process visibility:

1. Find FIX 49's mem_watcher implementation (likely `scripts/brain_worker.py` or a shared helper). `grep -nE "mem_watcher|FIX 49" -r app/ scripts/` to locate.
2. Make it importable from `app/services/diagnostics/mem_watcher.py` if it isn't already shared.
3. Wire it into chili's startup — add a `@app.on_event("startup")` handler that spawns the watcher coroutine.
4. Match the cadence: every 60s, log RSS + `Counter(type(o).__name__ for o in gc.get_objects()).most_common(20)`.

The point is that chili's main process now self-reports its top object types every minute. After 30 minutes of running, the survivor counts that grow monotonically are the leak.

**Verification:**
- `docker compose restart chili`
- `docker compose logs chili --since 5m | grep -i "mem_watcher\|top types"` shows the per-minute lines.
- Wait 30 min, then check the survivor diff between t=1min and t=30min.

### Commit 2: Surgical fix at the identified leak site

**File(s):** Whatever Commit 1's mem_watcher diagnostic identifies.

**What:**

Once the leaking object class is named (likely `function` / `cell` / `dict` / `list` / `Thread` / `Timer`), grep the codebase for the construction site:

```bash
grep -rnE "Thread\(|Timer\(|ThreadPoolExecutor\(|run_in_executor\(|httpx\.Client\(|requests\.Session\(|httpx\.AsyncClient\(" app/
```

Apply ONE of these surgical fixes depending on what's found:

**Pattern A — per-request executor / session / client:**
```python
# Before (in some endpoint or handler):
def handle_thing():
    client = httpx.Client()  # creates new connection pool + thread per call
    result = client.get(...)
    # client never explicitly closed; Thread leaks

# After:
# Hoist to module scope or app.state:
_HTTP_CLIENT = httpx.Client()  # one per process

def handle_thing():
    result = _HTTP_CLIENT.get(...)
```

**Pattern B — uncancelled `threading.Timer`:**
```python
# Before:
def schedule_retry(...):
    t = threading.Timer(5.0, fn)
    t.start()
    # t never .cancel()'d on success; survives even when fn ran

# After:
# Use asyncio.create_task with explicit cleanup, OR maintain a registry of
# active timers and cancel-on-completion.
```

**Pattern C — unbounded cache (FIX 50 style):**
```python
# Before:
_cache: dict[str, Any] = {}

def get_cached(key):
    if key not in _cache:
        _cache[key] = compute(key)
    return _cache[key]

# After:
from functools import lru_cache
# Or: cachetools.TTLCache with explicit max + ttl.
```

**Constraint:** Don't apply a sweeping cap (e.g., "all dicts limited to N"). Find the SPECIFIC holder, fix the SPECIFIC site.

**Decision branches:**
- **A. Single clear holder identified.** One commit, surgical fix.
- **B. Multiple smaller leaks compounding.** Fix the largest one; document others for f-leak-3.
- **C. Holder identified but the fix requires a structural change.** Document; defer the fix to its own brief; close f-leak-2 with the diagnosis only.

**Verification:**
- `docker compose restart chili`
- Wait 30 min observing mem_watcher.
- Compare RSS slope and top-type survivors before vs after the fix.
- Target: slope ≤ 1/10 pre-fix slope on the previously-leaking type.
- Reference data: f-leak-1's stats-logger has the pre-fix curve; this task's mem_watcher has the post-fix curve.

### Commit 3 (optional): Same fix in scheduler-worker if the pattern matches

**File(s):** Likely the same code path that's affecting both processes (chili and scheduler-worker share most of `app/`).

**What:**

scheduler-worker's mem_watcher already showed +14/min Thread-closure survivors. If Commit 2's fix happens to land in shared code (under `app/`), the leak is fixed in both places automatically. If chili's leak is in chili-only code (e.g., a router), scheduler may have a separate but similar leak.

If the fix is shared-code: confirm scheduler-worker also benefits via its existing mem_watcher logs (compare survivor counts pre/post).

If chili-only: document scheduler-worker's leak for follow-up; don't refactor in this task.

**Decision:** This commit only happens if Commit 2's fix is in shared code AND the matching pattern is also visible in scheduler-worker.

## Brain integration (reuse, don't rewrite)

- `mem_watcher` from FIX 49 — lift, don't rebuild.
- `dispatch-stats-logger.ps1` from f-leak-1.1 — already running, gives the system-level RSS curve.
- `dispatch-stats-trend.ps1` from f-leak-1.1 — read this for "is the slope reduced?" verification.
- FIX 50's structural pattern (`_ind_cache` + LRU/TTL) — the template for any cache-class fix.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Untouched.
- **Default mode stays paper.**
- **No strategy threshold tuning.**
- **No migrations.**
- **No miner / scanner / executor / gate code changes.** chili's leak is in request-handling or app-startup territory, not the fast-path subsystem.
- **No fast-data-worker changes.** Clean, stable, leave alone.
- **No restart of fast-data-worker** during this task — it would interrupt the F8a soak that's still accumulating data for f8a-evaluation-rerun-2.
- **`models/trading.py`, `.env.example`, executor, exit_manager, gate stack, calibration helpers.** Continue to leave alone.
- **The chili docker-compose memory limit (3 GiB)** stays. Increasing it would just make restart-cycles take longer — doesn't fix the leak.

## Out of scope

- Refactor of chili's request handling.
- Refactor of mem_watcher itself.
- Migrating mem_watcher to a shared metrics endpoint.
- Investigating brain-worker's CPU 109% (f-leak-1 found it's a known FractionalBacktest, not a regression).
- Fixing the scheduler-worker leak (unless trivially shared-code with chili — Commit 3 branch).
- f8a-evaluation-rerun-2 / F9 / f-leak-3 / f-hygiene-3 — all wait until the leak is fixed.
- The structural `fast_alerts` duplicate-microsecond pattern (deferred from f-leak-1's review; future structural pass).
- Repairing any orphaned rows (f-leak-1 found none in critical tables).

## Success criteria

1. `git log --oneline -5` shows up to 3 new commits, pushed to origin. Commit 1 (mem_watcher lift) is required; Commit 2 (fix) is required if a holder is identified; Commit 3 (scheduler echo) is optional.
2. After Commit 1 lands and chili restarts: `docker compose logs chili --since 5m | grep "top types"` shows per-minute mem_watcher lines.
3. The CC report `docs/STRATEGY/CC_REPORTS/<date>_f-leak-2.md` includes:
   - The specific Python type / closure / cache that was leaking, with growth rate (objects/min).
   - The specific code path that holds it (file + line).
   - The specific surgical fix applied (or "deferred to f-leak-3" with rationale per branch C).
   - Pre/post mem_watcher survivor counts demonstrating the slope reduction.
4. Post-fix slope ≤ 1/10 pre-fix slope on the previously-leaking type, OR the report includes branch C documentation explaining why this couldn't be achieved in a single session.
5. F8a soak continues uninterrupted on fast-data-worker. No behavioral changes to any strategy code path.
6. Total committed-container memory after fix: chili stable at <50% of its 3 GiB cap over a 1h window (target: ≤ 1.5 GiB).

## Open questions for Cowork (surface in your report only if relevant)

1. **If Commit 2's fix is in shared code under `app/`, scheduler-worker's existing leak should also benefit.** Confirm via scheduler's mem_watcher logs (compare survivor counts pre/post fix). If it doesn't benefit, scheduler has a separate but similar leak — flag for f-leak-3.

2. **If the leak is event-driven (e.g., fires on specific user actions or specific scheduler jobs)** rather than a steady drip, mem_watcher might not show monotonic growth in a 30-min window. Use the dispatch-stats-logger time series from f-leak-1.1 (which has been running for hours by now) to identify which 30-min windows DID show growth, and run mem_watcher during a known-growth window if possible.

3. **If the holder is identified as `httpx.Client()` per request**, switching to a single module-level client may interact with chili's async/sync boundary. httpx's sync `Client` and async `AsyncClient` are not interchangeable. If chili's request handlers are mostly async, use `AsyncClient` and ensure a single instance per event loop.

4. **If branch C (defer the fix)** — the task still ships value via Commit 1 (mem_watcher in chili = future-proof diagnostic). Document the surfaced findings carefully; the next session works from a much better starting point.

## Rollback plan

- Commit 1 (mem_watcher lift): purely additive. Revert removes the diagnostic; no behavioral change.
- Commit 2 (fix): targeted file change. Revert restores prior behavior; chili leak resumes; WSL2 cap from f-leak-1 still protects the host.
- Commit 3 (optional shared-code echo): same as Commit 2.
- No migrations. No data migrations. No schema changes.
- No live-placement risk: none of these touch the executor, gates, broker code, or strategy thresholds.
