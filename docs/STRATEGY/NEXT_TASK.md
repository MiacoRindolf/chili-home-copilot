# NEXT_TASK: f-leak-4

STATUS: DONE

## Goal

Fix three independent memory leaks identified in mem_watcher data
post-deploy. Each phase is small and isolated; after this ships,
the post-deploy memory profile should hold steady at:

- scheduler-worker < 500 MB stable (currently growing 9-15k
  ReferenceType weakrefs per 5min tick, climbing toward 3 GB)
- chili main app < 600 MB stable (currently +63 MB/min slope; 3.7 GB/hr
  if sustained)
- brain-worker < 800 MB stable (currently churning 9 backtests/sec;
  BookLevel/NumpyBlock pandas allocations not GC'd cleanly)

f-leak-3 (yfinance Thread-closure leak) is verifiably HOLDING —
`invoke_excepthook` count 0 across all three containers, yf-breaker
state CLOSED. **The new leak signature is different.** Three distinct
shapes, each in code that landed THIS WEEK during the exit-engine /
brain-correctness chain. Surgical fix per shape.

## Why now

Operator captured pre/post-restart mem_watcher data after today's
deploy:

```
SCHEDULER-WORKER (heaviest leak):
  Pre-restart 08:01:   vm_rss=3035MB  py_objects=820k  ReferenceType=191k
  Post-restart 08:09:  vm_rss=200MB   py_objects=283k
  → Pre-restart growth: ReferenceType +9k-15k per 5min tick

CHILI MAIN APP:
  Pre-restart 08:07:   vm_rss=1433MB  threads=72(!)  py_objects=834k
  Post-restart 08:09:  vm_rss=400MB   py_objects=686k
  +1min later:         vm_rss=463MB (+63MB!) py_objects=730k (+44k)
  → 63 MB/min sustained = 3.7 GB/hr

BRAIN-WORKER:
  fast_backtest_loop tick: completed=1362 errors=0 elapsed=145.9s
  → ~9 backtests/sec; each FractionalBacktest allocates BookLevel /
    NumpyBlock pandas structures
```

f-leak-3 holding (good): `invoke_excepthook` 0/0/0; yf-breaker CLOSED.
**f-leak-4 is a separate, multi-source leak.** The fastest probable
root cause is the SessionLocal sites added today (commits f87e62a,
e2a974e). If any of them does `db = SessionLocal()` then `db.add()`
without `with` or explicit `try/finally db.close()`, the ORM
identity-map weakrefs accumulate forever — exactly the
ReferenceType pattern.

This blocks the day's push: 14 commits worth of architectural
correctness shouldn't ship to remote while a leak shipped from those
commits is actively eroding host memory.

## Phase ordering and stop-on-blocker policy

**Sequence (highest probability of mechanical fix first):**

1. **Phase 1 — SessionLocal context-manager audit** (~30 min)
   Audit the three new SessionLocal sites. If any uses bare
   `db = SessionLocal()` without context manager, fix in place.
   This is the highest-probability cause of the scheduler-worker
   ReferenceType leak.

2. **Phase 2 — FastAPI middleware request-retention probe** (~60 min)
   Diagnostic-then-fix for chili's per-request closure leak. Find
   what's holding `request_response.<locals>.app` /
   `get_request_handler.<locals>.app` references.

3. **Phase 3 — FractionalBacktest strategy-instance cleanup** (~45 min)
   Verify FractionalBacktest tearsdown its strategy instance +
   DataFrames after each run. The `_parity_sink` from f-exit-parity-persist
   is a candidate retention site (drains at end-of-run, but the empty
   list stays attached).

**Stop-on-blocker policy:** Each phase commits independently. If a
phase doesn't reproduce the leak in the diagnostic step, commit a
"verified non-issue" note in the CC report and skip to the next.
Don't deadlock waiting on operator input.

**Goal**: ship as many fixes as possible. Acceptable floor: at least
Phase 1 ships (highest-probability surgical fix). After all phases,
operator deploys + verifies via mem_watcher trend, then pushes the
14-commit backlog.

---

# Phase 1 — SessionLocal context-manager audit

## Goal

Three SessionLocal sites added in today's commits. Audit each. If
any leaks weakrefs by skipping context-manager / explicit close,
fix in place.

## Source material

Three suspect sites (per the operator's diagnosis):

1. **`app/services/broker_service.py`** — Phase 1 shadow-mode write
   helpers from commit `e2a974e` (position-identity-phase-1). Find
   the SessionLocal calls in `sync_positions_to_db` (~line 1372).

2. **`app/services/trading/live_exit_engine.py::_phase_b_shadow_parity`**
   — commit `f87e62a` (f-exit-parity-persist). The function does
   `db.add(row); db.commit()` per the f-exit-parity-persist brief's
   Step 2.

3. **`app/services/trading/backtest_service.py::_drain_backtest_parity_sink`**
   — commit `f87e62a`. Drains `_parity_sink` to ExitParityLog at
   backtest-run completion.

For each site, check whether `SessionLocal()` is wrapped in:
- ✅ `with SessionLocal() as db:` (correct — auto-closes)
- ✅ `try:` ... `finally: db.close()` (correct — explicit close)
- ❌ Bare `db = SessionLocal()` ... `db.commit()` ... `(no close)` (LEAKS)

## Path

### Step 1.1 — Audit each site

Read each function. Identify the SessionLocal lifecycle. Document
findings in the CC report's per-phase section.

### Step 1.2 — Fix any leakers

For any site found to use bare SessionLocal without close:

**Pattern A — convert to context manager (preferred):**

```python
# OLD (leaks weakrefs)
db = SessionLocal()
db.add(row)
db.commit()

# NEW
with SessionLocal() as db:
    db.add(row)
    db.commit()
```

**Pattern B — explicit close (when context manager doesn't fit):**

```python
db = SessionLocal()
try:
    db.add(row)
    db.commit()
finally:
    db.close()
```

Either is acceptable. Pattern A is preferred per Python idiom.

### Step 1.3 — Tests

Per fixed site, add a test:

```python
# tests/test_<module>_session_lifecycle.py

def test_<function>_does_not_leak_session(...):
    """Verify SessionLocal opened in <function> closes after return."""
    import gc, weakref
    from app.db import SessionLocal
    sessions_before = sum(1 for o in gc.get_objects()
                           if type(o).__name__ == 'Session')
    <call the function>
    gc.collect()
    sessions_after = sum(1 for o in gc.get_objects()
                          if type(o).__name__ == 'Session')
    assert sessions_after <= sessions_before, \
        f"Session leak: {sessions_before} → {sessions_after}"
```

### Step 1.4 — Smoke verification

Operator-side, post-deploy:

```sql
SELECT application_name, state, COUNT(*) AS n
FROM pg_stat_activity
WHERE application_name LIKE 'chili%'
GROUP BY application_name, state
ORDER BY application_name, state;
```

Expected: idle counts stay flat over 30 min; idle-in-tx stays at 0
after each tick completes.

## Success criteria

- Each of 3 sites has a documented audit verdict
  (correct / leaking / fixed).
- Per-site test guards the lifecycle going forward.
- Smoke shows scheduler-worker `pg_stat_activity` rows stable.

## Commit message

`fix(session-leak): close ORM session lifecycle in 3 new shadow-write sites (f-leak-4 phase 1)`

---

# Phase 2 — FastAPI middleware request-retention probe

## Goal

Chili main app is leaking 63 MB/min. mem_watcher's top qualnames:

```
request_response.<locals>.app = 1279
get_request_handler.<locals>.app = 1275
set_model_mocks.<locals>.attempt_rebuild_fn.<locals>.handler = 1488
```

These are per-request closures. Their counts grow proportional to
HTTP request volume. Either a middleware retains references to
handler closures across requests, OR pydantic's model-rebuild path
retains rebuilds.

## Source material

- `app/main.py` — FastAPI app + middleware setup. Find every
  `app.add_middleware(...)` call.
- `app/routers/` — every route module. Pydantic `BaseModel` definitions
  in request/response shape.
- `set_model_mocks` is from pydantic v2's deferred-validation path.
  Used when models reference each other lazily and need rebuild.

## Path

### Step 2.1 — Diagnostic: list every middleware

```python
# Inspect app.user_middleware (the registered middleware list)
docker compose exec chili python -c "
from app.main import app
for mw in app.user_middleware:
    print(f'{mw.cls.__module__}.{mw.cls.__name__}')
"
```

Each entry is a candidate retention site. Read the `dispatch` method
of each. Look for:
- Stores request / response in `self` or a global
- Caches keyed on something request-bound
- Logs that retain request metadata

### Step 2.2 — Diagnostic: count references to a recent request closure

```python
# Trigger 100 requests, then check closure counts.
# Operator-side via curl or via the Autopilot UI's natural traffic.
# Check before + after:
docker compose exec chili python -c "
import sys
mods = sys.modules
counts = {}
for name, mod in mods.items():
    for attr in dir(mod):
        try:
            obj = getattr(mod, attr, None)
            qn = getattr(obj, '__qualname__', '')
            if 'request_response' in qn or 'get_request_handler' in qn:
                counts[qn] = counts.get(qn, 0) + 1
        except Exception:
            pass
print(counts)
"
```

If counts grow with request volume, it's a real leak. If they stay
flat, the closures are short-lived and the leak is elsewhere.

### Step 2.3 — Fix candidates

If a middleware is found to retain references:

- **Replace store-in-self with store-in-state**: middleware-instance
  state shouldn't hold per-request data. Use `request.state` for
  per-request storage that's released with the request.
- **Replace LRU caches keyed on request**: rekey on something
  immutable (URL path, user_id) instead of the request object.

For pydantic `set_model_mocks`: if model rebuild is firing per
request, find the model with deferred validation and either eagerly
rebuild at import time (`Model.model_rebuild()`) or restructure the
field to not need lazy rebuild.

### Step 2.4 — Tests

If a fix is identified, add a test that:
- Triggers N requests against a synthetic test client
- Asserts closure count post-N stays within N+constant of pre-N
- Specifically: not N×constant (which would indicate per-request
  retention)

### Step 2.5 — Smoke verification

```python
# After deploy + 5 min of traffic, check chili mem_watcher logs:
docker compose logs chili --since 5m | Select-String "mem_watcher" |
    Select-String "request_response|get_request_handler"
```

Expected: closure count grows initially (cold start) then plateaus.
NOT: monotonic growth proportional to time/requests.

## Success criteria

- Middleware audit produces a concrete list of candidate retainers.
- Either a fix lands or the diagnostic confirms middleware is clean
  and surfaces the actual cause.
- Smoke shows chili memory slope drops materially below 63 MB/min.

## Commit message

`fix(fastapi-leak): release per-request closures (f-leak-4 phase 2)`

OR if no fix lands:

`docs(audit): chili-app per-request closure investigation (f-leak-4 phase 2)`

---

# Phase 3 — FractionalBacktest strategy-instance cleanup

## Goal

`fast_backtest_loop` runs ~9 backtests/sec. Each instantiates a
DynamicPatternStrategy + pandas DataFrames (BookLevel, NumpyBlock).
If even a small fraction don't get GC'd between runs, the cumulative
memory pressure is hundreds of MB/hr.

f-exit-parity-persist's `_parity_sink` (drained at run completion)
is a candidate retention site: the sink is a list attached to the
strategy instance. After drain, the sink is empty, but the strategy
instance + its DataFrames are still referenced by whatever holds the
strategy.

## Source material

- `app/services/backtest_service.py::_drain_backtest_parity_sink` —
  added in commit `f87e62a` (f-exit-parity-persist).
- `app/services/backtest_service.py::DynamicPatternStrategy` — the
  legacy backtest path's strategy class.
- `scripts/brain_worker.py::_run_fast_backtest_independent_loop` —
  the FIX 34 independent loop that runs backtests at 9/sec.
- The mem_watcher tick's `top_delta_since_last`: previously caught
  `BookLevel +944` in one tick — that's the smoking gun for pandas
  retention.

## Path

### Step 3.1 — Diagnostic: where is the strategy instance referenced?

After a backtest completes, what holds a reference to the
DynamicPatternStrategy instance?

Run a quick diagnostic with `gc.get_referrers`:

```python
docker compose exec brain-worker python -c "
import gc
from app.services.backtest_service import DynamicPatternStrategy
instances = [o for o in gc.get_objects() if isinstance(o, DynamicPatternStrategy)]
print(f'Live DynamicPatternStrategy instances: {len(instances)}')
if instances:
    refs = gc.get_referrers(instances[0])
    print(f'Referrers of first instance: {len(refs)}')
    for r in refs[:5]:
        print(f'  {type(r).__name__}: {repr(r)[:100]}')
"
```

If `len(instances) > N` where N is the expected concurrent runs (~1
per worker thread), there's accumulation. Trace the referrers to
find the holder.

### Step 3.2 — Likely root causes

Three candidates, in order of probability:

**(a) The `_parity_sink` is set in `__init__` and never reset.**
  Even after drain, the sink list stays attached. Fix: set
  `strategy._parity_sink = None` (or `del`) after drain in
  `_drain_backtest_parity_sink`.

**(b) The fast_backtest_loop holds a reference to the last N
  strategy instances in some bookkeeping list.** Check
  `_run_fast_backtest_independent_loop` for bookkeeping that
  retains strategy references.

**(c) pandas DataFrame views**: if strategy stores a slice / view of
  the parent OHLCV DataFrame, the parent stays alive as long as the
  view exists. Fix: ensure strategy holds copies (`.copy()`) of any
  data it needs to outlive the parent, OR explicitly del the parent
  after the strategy finishes.

### Step 3.3 — Fix

Per Step 3.2's diagnosis:

**For (a)** — in `backtest_service.py::_drain_backtest_parity_sink`:

```python
# After successful drain:
strategy._parity_sink = None
```

This breaks the back-reference; the strategy can now be GC'd when
the run-loop releases its reference.

**For (b)** — in `brain_worker.py::_run_fast_backtest_independent_loop`:

If a `recent_runs` / `last_strategies` list exists, cap it at N and
explicitly drop older entries.

**For (c)** — in `DynamicPatternStrategy.__init__` or the harness:

If pandas views are retained, force `.copy()` on the data passed in
so the parent is freeable.

### Step 3.4 — Tests

```python
# tests/test_fast_backtest_loop_cleanup.py

def test_fast_backtest_does_not_retain_strategy_instances():
    """Verify each fast_backtest tick releases its strategy instance."""
    import gc
    from app.services.backtest_service import DynamicPatternStrategy

    before = sum(1 for o in gc.get_objects()
                  if isinstance(o, DynamicPatternStrategy))
    # Run 10 backtests via the harness
    for _ in range(10):
        run_one_fast_backtest_tick()  # synthetic
    gc.collect()
    after = sum(1 for o in gc.get_objects()
                 if isinstance(o, DynamicPatternStrategy))
    # Allow ≤ 2 lingering (e.g., one currently running + 1 pool slot)
    assert after - before <= 2, \
        f"DynamicPatternStrategy leak: {before} → {after}"
```

### Step 3.5 — Smoke verification

```python
# Trigger 100 fast_backtest ticks, observe mem_watcher BookLevel /
# NumpyBlock counts:
docker compose logs brain-worker --since 5m | Select-String "mem_watcher" |
    Select-String "BookLevel|NumpyBlock|DynamicPatternStrategy"
```

Expected: BookLevel + NumpyBlock counts plateau within a tick,
not monotonic growth.

## Success criteria

- Diagnostic confirms or rules out each of the 3 candidate causes.
- Identified leak source has a 1-line surgical fix.
- Tests pin the lifecycle going forward.
- Smoke shows brain-worker memory slope flattens during a fast_backtest run.

## Commit message

`fix(fast-backtest-leak): release strategy instance after parity drain (f-leak-4 phase 3)`

OR if no leak found:

`docs(audit): fast-backtest cleanup investigation, no leak found (f-leak-4 phase 3)`

---

# Combined CC report

After all phases, write `docs/STRATEGY/CC_REPORTS/<date>_f-leak-4.md`
covering:

- Per-phase status (SHIPPED / VERIFIED-NON-ISSUE / BLOCKED)
- Per-phase audit findings + commit hash
- Cross-phase observations (any of the three leaks share a root cause?)
- Smoke verification: post-deploy mem_watcher tick comparison vs
  pre-fix.

## Constraints / do not touch (cross-phase)

- **Default mode stays paper.** Live placement default unchanged.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **Do not modify run_learning_cycle re-enable.** Stays gated off.
- **Do not modify any of the 6 brain_work handlers.** They're verified
  clean.
- **Do not modify the canonical evaluator** (`exit_evaluator.py`).
- **Do not modify the realized-EV gate** (`realized_ev_gate.py`).
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No migrations.** Pure code-side leak fix.
- **No `git push --force`.** PROTOCOL Hard Rule 4.
- **One commit per phase.** Atomic recoverability.
- **If a phase has no leak**, document the negative finding cleanly
  and move to the next.

## Out of scope

- Auto-promote shadow → authoritative for the realized-EV gate.
- Live ↔ shadow exit-time syncing.
- UI / dashboard for execution-alpha-drag.
- Backtest ↔ shadow comparison.
- The PED bracket-writer bug (operator flagged separately).
- Any other follow-up brief from prior reviews.

## Success criteria (cross-phase)

1. **At least Phase 1 ships** (highest-probability surgical fix).
   Aspirational: all 3 phases ship.
2. **Each phase commits independently.**
3. **Combined CC report covers all 3 phases honestly** (SHIPPED /
   VERIFIED-NON-ISSUE / BLOCKED + reasoning).
4. **Post-fix mem_watcher slopes drop materially:**
   - scheduler-worker ReferenceType growth: ≤ 1k per 5min tick
     (currently 9-15k)
   - chili memory slope: ≤ 10 MB/min (currently 63 MB/min)
   - brain-worker BookLevel count: plateaus per tick
5. **No regression** on existing tests.

## Rollback plan

- Per-phase commits → `git revert <phase-commit>` rolls back just that
  phase.
- No data, no schema, no migration changes — all rollbacks are pure
  code.
- f-leak-3 stays in place regardless (not modified by this brief).

## Open questions for Cowork (surface in CC report only if relevant)

1. **Cross-phase root cause** — could the three leaks share a single
   underlying cause (e.g., a session lifecycle pattern that affects
   multiple paths)? If yes, surface and suggest a unified fix.

2. **6pm job cluster** — operator noted that `pattern_regime_perf_daily`
   (23:00 UTC), `pattern_regime_killswitch_daily` (23:05),
   `learning_cycle` (22:30), `brain_market_snapshots` (22:19) are
   evening heavy-lifters that "likely accelerate all three leak
   surfaces during their run window." If the diagnostic captures
   data during one of those windows, the leak slope numbers above
   will be 2-5× higher. Surface honestly.

3. **Push-blocking** — the day's 14 commits should NOT be pushed
   until f-leak-4 ships and verifies. Surface explicitly: f-leak-4
   gates the push.

4. **Threshold for "verified non-issue"** — if Phase 2 or 3 finds no
   reproducible leak in the diagnostic window, what's the
   confidence threshold for declaring it not-an-issue? Recommend:
   if 30+ min of post-restart traffic shows no closure / instance
   accumulation, declare non-issue and move on. Surface in the
   report.

5. **Other recently-shipped SessionLocal sites** — if Phase 1 finds
   one of the three suspects is leaking, audit ALL SessionLocal
   sites added in the last 7 days for the same pattern. The leak
   class might extend beyond the three documented suspects.
