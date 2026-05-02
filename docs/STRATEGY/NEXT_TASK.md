# NEXT_TASK: f-hygiene-1

STATUS: DONE

## Goal

Three small soak-safe hardening items, bundled into one task with three commits. None of these change strategy behavior or thresholds; all three address visibility and safety gaps surfaced across the last several runs (F6, cleanup-2, F8a-fix). The F8a soak window keeps running in parallel and will accumulate organic data while these land.

After this task:

1. **Decay miner failures become visible.** A watchdog task in the supervisor monitors the `decay_miner` asyncio task and surfaces a heartbeat / restart signal when it dies silently. Deferred since F6.
2. **`last_error` in `fast_path_status` self-clears after sustained recovery.** A stale error from a transient hiccup at startup no longer haunts the operator dashboard for hours. Deferred since cleanup-2.
3. **The scanner drain invariant is `-O`-safe.** Replaces the `assert obs.ticker == triggering_ticker` in `_drain_pullback_due` with an explicit `if/raise RuntimeError`, so the invariant survives even if the container ever runs Python with optimizations on. Surfaced in F8a-fix as Open Question 4.

Three commits, all soak-safe, no strategy impact, no calibration retuning.

## Why now

- The F8a fix is verified (5.4% → 100% capture rate) and the soak window is running. Organic data accumulation needs ~24h to be statistically meaningful — that's the bottleneck for F8a evaluation, not engineering work. We can either sit idle or land soak-safe hardening that we'll want anyway.
- All three items are deferred work that's been waiting for a slot. None of them modifies any threshold, gate, or trading-logic path — they're hygiene around the already-working subsystem.
- The `-O` safety item is fresh from F8a-fix's open question. Folding it in now while the context is hot is cheaper than reopening the file in a future task.

## Architectural commitments

- **Event-driven shape preserved.** Watchdog uses asyncio task `done()` / `exception()` introspection on a low-frequency timer (e.g., every 60s). No polling-of-database, no extra LISTEN channels.
- **No new magic numbers.** Constants introduced (watchdog interval, recovery window for `last_error` clearing) are explicit named constants at module top with comments — not buried literals.
- **Idempotent, additive changes.** Every subtask is additive: deletion only happens for the `assert` line being replaced. No deletions of behavior.
- **No miner / scanner / executor / gate changes** beyond the three explicit edits below.
- **No migrations.** No schema work.
- **Three commits.** Each subtask = one logical commit.

## Scope — three commits

### Commit 1: Watchdog on `decay_miner` asyncio task

**File:** `app/services/trading/fast_path/supervisor.py` (and any small status-surface helper if needed)

**What:**

The supervisor already starts `decay_miner` as a long-lived asyncio task. Today, if it crashes inside the LISTEN loop (e.g., a Postgres reconnect bug, an unexpected payload shape, a lib upgrade), the task ends silently and no further `fast_signal_decay` rows are written. The supervisor doesn't notice because nothing is watching.

Add a watchdog coroutine that runs alongside the supervisor loop:

```python
WATCHDOG_INTERVAL_S = 60.0  # how often to check task health

async def _decay_miner_watchdog(task: asyncio.Task, status_tracker: StatusTracker) -> None:
    """Surface silent decay_miner failures via fast_path_status.last_error."""
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL_S)
        if task.done():
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                logger.warning("[fast_path] decay_miner watchdog: task was cancelled")
                status_tracker.record_error("decay_miner", "task cancelled")
                return
            if exc is not None:
                logger.error(
                    "[fast_path] decay_miner watchdog: task died with %s: %s",
                    type(exc).__name__, exc,
                )
                status_tracker.record_error(
                    "decay_miner", f"{type(exc).__name__}: {exc}"
                )
            else:
                logger.warning("[fast_path] decay_miner watchdog: task ended without exception")
                status_tracker.record_error("decay_miner", "task ended unexpectedly")
            return
```

Wire it in the supervisor's task-launch block:

```python
decay_task = asyncio.create_task(decay_miner.run(...))
asyncio.create_task(_decay_miner_watchdog(decay_task, status_tracker))
```

**Status surface:** `fast_path_status.last_error` (existing field) is the right channel — operator dashboard already surfaces it. No new fields required. The existing `record_error` helper is reused.

**Restart policy:** Out of scope. The watchdog *reports*; it doesn't *restart*. A restart policy is its own decision (do we want infinite retries? exponential backoff? circuit breaker?) and should not be folded into a hygiene pass. Reporting is the soak-safe minimum.

**Verification:**
- Boot fast-data-worker; confirm `[fast_path] decay_miner watchdog` startup log line is present.
- (Optional, manual) — kill the decay_miner task in a Python REPL via `task.cancel()` in a test harness; confirm `last_error` populates within 60s.

### Commit 2: Clear stale `last_error` in `fast_path_status` after sustained recovery

**File:** `app/services/trading/fast_path/status_tracker.py` (or wherever `last_error` is owned — locate via grep on `last_error` and `fast_path_status` if unsure)

**What:**

Today, `last_error` is set on any error and never cleared on recovery. A transient hiccup at startup (e.g., a single 1009 frame, a brief reconnect) leaves the operator dashboard showing an error for the rest of the day even though the system has been streaming normally for hours. Deferred since cleanup-2.

Add a sliding-window recovery clear:

```python
ERROR_CLEAR_AFTER_HEALTHY_MIN = 5.0  # clear last_error after 5 min of clean streaming

class StatusTracker:
    ...
    def record_healthy_tick(self) -> None:
        """Called by the WS streaming loop on each successful book/bar emit."""
        now = time.monotonic()
        if self._last_error is None:
            self._last_error_cleared_at = now  # nothing to clear; just refresh
            return
        if self._last_healthy_streak_started is None:
            self._last_healthy_streak_started = now
        elif (now - self._last_healthy_streak_started) >= ERROR_CLEAR_AFTER_HEALTHY_MIN * 60.0:
            logger.info(
                "[fast_path] status_tracker: clearing stale last_error after %.1f min healthy streak",
                ERROR_CLEAR_AFTER_HEALTHY_MIN,
            )
            self._last_error = None
            self._last_error_at = None
            self._last_healthy_streak_started = None

    def record_error(self, source: str, msg: str) -> None:
        # existing behavior plus:
        self._last_healthy_streak_started = None
```

Hook `record_healthy_tick()` into the existing WS streaming loop wherever each book/bar is successfully delivered.

**Why 5 minutes specifically:** This is a UX recovery threshold, not a strategy threshold. 5 minutes is long enough that a flapping connection won't keep clearing-and-resetting (the next error within 5 min restarts the streak), and short enough that the operator sees recovery on the same dashboard refresh. Document the choice inline.

**Note this is NOT a magic number in the F6 sense.** A magic number would be something like "trading-cost threshold = 0.05%" — a strategy parameter that should be calibrated from data. This is operator UX timing, on the same shelf as "auto-refresh dashboard every 30s." Different category.

**Verification:**
- Trigger an error (e.g., kill a fixture briefly), observe `last_error` populates.
- Wait 5 minutes of clean streaming, observe `last_error` clears with the log line above.

### Commit 3: Replace `assert` with `if/raise RuntimeError` in scanner drain

**File:** `app/services/trading/fast_path/scanner.py`

**What:** Single-line surgical change in `_drain_pullback_due`:

```python
# Before
assert obs.ticker == triggering_ticker, (
    f"_drain_pullback_due invariant violated: heap key "
    f"{triggering_ticker} contained entry for {obs.ticker}"
)

# After
if obs.ticker != triggering_ticker:
    raise RuntimeError(
        f"_drain_pullback_due invariant violated: heap key "
        f"{triggering_ticker} contained entry for {obs.ticker}"
    )
```

**Why:** Python `-O` flag strips `assert` statements. The fast-data-worker container doesn't currently use `-O` (verified in F8a-fix), but if anyone ever flips it (perf tuning, base-image change, build-arg drift), the per-ticker invariant guard becomes a silent no-op and we re-introduce exactly the silent-data-corruption mode F8a-fix was meant to close. `raise RuntimeError` survives `-O`.

**Verification:** Boot fast-data-worker; observe at least one natural drain wave; confirm no behavioral difference (the invariant should never trip in practice — that's the point).

## Brain integration (reuse, don't rewrite)

- `StatusTracker` (commit 2) — extend in place with `record_healthy_tick` + the streak-tracking fields. Don't subclass.
- Supervisor (commit 1) — add the watchdog coroutine alongside existing task launches. Same pattern as the other supervisor tasks.
- Scanner (commit 3) — surgical line replacement, nothing else.

## Constraints / do not touch

- **All 8 live-placement safety belts.** No changes to executor, gates, or live-flag logic.
- **Default mode stays paper.**
- **No strategy threshold tuning.** Don't touch `VOL_BREAKOUT_MULT`, `VOL_BREAKOUT_PULLBACK_DELAY_S`, `MAX_PENDING_DEFERRED`, the calibration constants, or any gate threshold.
- **No miner code changes** beyond the watchdog wrapper. The watchdog *observes* the miner task; it doesn't reach into the miner's logic.
- **No migrations.**
- **No new gates.**
- **No restart policy on decay_miner.** Watchdog reports; restart policy is a separate, future decision.
- **`models/trading.py` and `.env.example`.** Continue to leave them alone.

## Out of scope

- Restart policy / supervised retry on decay_miner crash (future task once we see one in the wild and decide on the right policy).
- Watchdogs on other asyncio tasks (scanner, ws_listener, exit_manager). Same pattern would apply, but doing all of them in one go is a refactor; one at a time on a "found a real failure mode here" basis is cleaner.
- F8b (calibrating `VOL_BREAKOUT_PULLBACK_DELAY_S` from data once organic firings accumulate).
- F9 (new signal types).
- Lazy eviction of expired-but-undrained heap entries (still deferred from F8a-fix).
- Any tuning of any threshold.

## Success criteria

1. `git log --oneline -5` shows three new commits, pushed to origin, with messages clearly identifying subtask (`feat(fast-path): decay_miner watchdog`, `fix(fast-path): clear stale last_error after healthy streak`, `chore(fast-path): -O-safe scanner drain invariant`).
2. `docker compose ps fast-data-worker` healthy after deploy.
3. `docker compose logs fast-data-worker --since 2m` shows the new watchdog startup log line for decay_miner.
4. After ≥5 minutes of clean streaming, `fast_path_status.last_error` is `NULL` (or whatever the cleared sentinel is) — verifiable via the supervisor metrics line or a direct DB query against `fast_path_status`.
5. Scanner drain path no longer contains `assert obs.ticker ==`; `grep -n "raise RuntimeError" app/services/trading/fast_path/scanner.py` shows the new guard in `_drain_pullback_due`.
6. F8a soak continues uninterrupted — no spurious decay_miner restarts, no spurious `last_error` populations, no behavioral changes to drain output.
7. `docs/STRATEGY/CC_REPORTS/<date>_f-hygiene-1.md` written following PROTOCOL.md format. Include:
   - The three commit SHAs and their diffs (file + LOC counts).
   - Verification of each subtask separately.
   - Any surprises or deviations.

## Open questions for Cowork (surface in your report only if relevant)

1. **Should the watchdog also restart the task** rather than just reporting? My instinct (and the brief): no — restart policy is its own decision and shouldn't be folded into a hygiene pass. But if you find evidence of a recoverable failure mode while implementing (e.g., a transient psycopg2 disconnect that's clearly safe to retry), flag it.

2. **5-minute recovery window for `last_error` clear.** This is operator-UX timing, not a strategy threshold, but if you see a reason it interacts with anything load-bearing (e.g., another subsystem reads `last_error` and treats absence as health signal), call it out.

3. **`record_healthy_tick` integration point.** I described it as "wherever each book/bar is successfully delivered." If you find a cleaner single chokepoint (e.g., one place after both bar and book successfully push to fast_snapshots / fast_orderbook), use it; if both are needed, hook both.

4. **The supervisor metrics line.** Currently shows error counts and heap depths. Worth adding a line entry for "watchdog healthy" / "watchdog reporting"? Not required; if it's one line of additive logging, fine; if it's a metrics-line refactor, defer.

## Rollback plan

- Each commit can be individually reverted. Commit 1 (watchdog) is purely additive — revert restores no-watchdog state. Commit 2 (last_error clear) — revert restores never-clear state. Commit 3 (assert→raise) — revert restores `assert`, which is what we had during F8a-fix's soak.
- No migrations. No data migrations. No schema changes.
- No live-placement risk: none of these touch the executor, gates, or any path that interacts with the broker.
