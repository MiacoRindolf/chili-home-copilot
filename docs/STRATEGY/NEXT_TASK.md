# NEXT_TASK: f-kill-legacy-learning-cycle

STATUS: DONE

## Goal

Eliminate `run_learning_cycle` as a load-bearing path in brain-worker.
The function and its 20+ in-process steps are LEGACY ARCHITECTURE
that was supposed to be replaced by the event-driven brain_work_events
handler migration (Phase 2). Per saved memory, only handler #1 (mine,
FIX 36) shipped on 2026-04-29; handlers #2-5 (cpcv_gate, promote,
demote, regime_ledger) stalled and the legacy cycle has been the
fallback ever since.

The legacy cycle has been catastrophically degraded for at least 24
hours: **0 of 18 cycles in the last 24h completed cleanly** (61%
silent TCP drops, 28% transaction-rolled-back, 0% clean). The cycle
holds a single PG session for 60-140 minutes per attempt, gets killed
by Docker NAT during long idles, and has been silently disabling every
downstream learning step including `update_pattern_stats_from_closed_trades`
(today's f-evidence-canonical-writer, which has therefore not fired
even once post-deploy).

This task ships the **immediate stop-the-bleeding** half of the
architectural cleanup:

1. Disable the legacy cycle invocation entirely in brain-worker.
2. Remove the cold-start carve-out so restarts don't trigger a cycle.
3. Audit which work-types are now uncovered (i.e., done by the cycle
   but not yet by event handlers) and surface them as a concrete
   migration backlog for Phase 2.
4. The brain becomes purely event-driven for what's covered; uncovered
   work-types are documented as "deferred until handler ships" with
   a short table of impact.

After this lands: brain-worker no longer crashes, downstream steps
that ARE event-handled (mine via handler #1, fast_backtest via FIX 34,
broker-sync via its own loop, scheduler jobs) keep running clean.
Steps that AREN'T yet handled (cpcv_gate, promote, demote,
regime_ledger, closed-trade pattern feedback aka
`update_pattern_stats_from_closed_trades`) stop running until their
handlers ship in Phase 2 follow-up briefs.

This is a deliberate trade: **temporary loss of some learning steps
in exchange for an actually-running brain-worker with no DB-drop
cascade.** The current state (cycle attempts work for 60-140 min,
crashes, retries, repeats) means none of those steps reliably run
anyway — disabling them just makes the silent failure honest.

## Why now

You said "cycles were legacy logic" and asked me to clean and fix
this as an algo trader. The diagnostic confirms the architecture is
in a half-migrated state:

| Architecture component | Status |
|---|---|
| FIX 31 cycle gate | shipped — but doesn't gate cold start or the 4h safety floor |
| Handler #1 (mine via FIX 36) | shipped 2026-04-29 |
| Handler #2 (cpcv_gate) | pending |
| Handler #3 (promote) | pending |
| Handler #4 (demote) | pending |
| Handler #5 (regime_ledger) | pending |
| `run_learning_cycle` deletion | pending — was supposed to follow handler #5 |
| Cold-start carve-out removal | pending |

Per `scripts/brain_worker.py:1054`:
```python
if _LAST_RECONCILE_PASS_AT is None:
    return False, "cold_start_first_cycle"
```
**Every brain-worker restart triggers a cycle.** And per line 1058:
```python
if elapsed >= _RECONCILE_PASS_MAX_INTERVAL_S:
    return False, f"safety_floor_elapsed_s={int(elapsed)}"
```
**Every 4 hours (default), a cycle is forced** even if no work
signals exist.

So even with the FIX 31 gate, the cycle runs at minimum every 4h +
every restart. Today's deploy of f-evidence-canonical-writer caused a
restart, which forced a cycle, which crashed at the 62-minute mark
on a TCP drop, which is why
`update_pattern_stats_from_closed_trades` (called at step 11 of the
cycle) never fired.

**Algo-trader framing**: the legacy cycle is dead code on life
support. Every minute it runs is a minute of compute spent on a path
that crashes 100% of the time. Every cycle attempt holds a session
that contributes to the brain-worker's idle-in-tx population. Every
restart gives it another chance to lock up. **Pull the plug.**

## Scope boundary

**In scope (Phase 1 — this brief):**
- Disable cycle invocation: kill switch at the call site, default
  off, env-flag to re-enable for emergency rollback.
- Remove the cold-start carve-out in `_should_skip_reconcile_pass`.
- Set the `_RECONCILE_PASS_MAX_INTERVAL_S` default to a sentinel
  (e.g., 0 or a very large number) that effectively disables the
  safety floor.
- Survey current event-handler coverage in `app/services/trading/`
  for `brain_work_events` consumers, identify which step-types are
  handled vs. not.
- Document the uncovered work backlog in
  `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` with one row per
  uncovered step + impact assessment.
- Smoke verification: brain-worker runs for 30+ minutes post-deploy
  with zero `learning_cycle_end` events.

**Out of scope (Phase 2 — separate briefs):**
- Shipping handler #2 (cpcv_gate). Separate brief
  `f-handler-cpcv-gate`.
- Shipping handler #3 (promote). Separate brief
  `f-handler-promote`.
- Shipping handler #4 (demote). Separate brief `f-handler-demote`.
- Shipping handler #5 (regime_ledger). Separate brief
  `f-handler-regime-ledger`.
- Wiring `update_pattern_stats_from_closed_trades` into an event
  handler. Separate brief `f-handler-pattern-stats` (depends on
  closed-trade event emission already firing — verify in the audit).
- Deleting `run_learning_cycle` source code from
  `app/services/trading/learning.py`. Separate cleanup brief once
  handlers #2-5 + pattern-stats handler all ship.
- Modifying `realized_ev_gate.py` or `promotion_gate.py`. They stay
  as-is; their inputs come from event handlers downstream.
- DB-stability config (TCP keepalives, pool_pre_ping). Out of scope
  here because the cycle disable removes the long-idle-transaction
  pattern that triggers the drops. If post-deploy data shows other
  long-running queries dropping connections, that becomes a
  separate brief; for now the cycle disable is sufficient.

## Brain integration / source material

- `scripts/brain_worker.py:1014-1105` — FIX 31 gate logic
  (`_should_skip_reconcile_pass`). The carve-out at line 1054 and
  safety floor at line 1058 are the two paths that force the cycle.
- `scripts/brain_worker.py:1107` (`_run_lean_cycle_loop`) — the loop
  that calls the gate + cycle. The kill switch lands here.
- `scripts/brain_worker.py:803` (`run_learning_cycle`) — the brain-worker
  wrapper that invokes the legacy cycle. **Do not delete in this brief.**
  Just stop calling it.
- `app/services/trading/learning.py:run_learning_cycle` — the legacy
  cycle itself. **Do not delete in this brief.** Stays callable for
  emergency rollback.
- `app/services/trading/learning.py:9582` — the call to
  `update_pattern_stats_from_closed_trades`. This step is INSIDE the
  cycle. After this brief lands, the call site is unreachable in
  normal operation. The function itself stays for the future
  `f-handler-pattern-stats` brief.
- `app/services/trading/learning_cycle_architecture.py` — if it
  exists per the .cursor/plans, it documents the cycle's step
  graph. Read it to enumerate which steps still need event-handler
  coverage.
- `scripts/brain_worker.py` — search for `brain_work_events`,
  `_dispatch_work`, `handler_` to find the existing event-driven
  surface. Use this to build the coverage audit.
- Saved memory `reference_phase2_event_handlers.md` — the original
  Phase 2 plan from 2026-04-29.
- Saved memory `reference_fix31_is_a_bridge.md` — the explicit
  framing that FIX 31 is a bridge, not a replacement.

## Path

### Step 1 — Kill switch on the cycle invocation

In `scripts/brain_worker.py:_run_lean_cycle_loop`, gate the
`run_learning_cycle()` call behind a settings flag. New env var
`CHILI_BRAIN_LEGACY_CYCLE_ENABLED` (default `0` / disabled):

```python
# In _run_lean_cycle_loop, around the existing skip_cycle check:

legacy_cycle_enabled = (
    os.environ.get("CHILI_BRAIN_LEGACY_CYCLE_ENABLED", "0").lower()
    in ("1", "true", "yes")
)

if not legacy_cycle_enabled:
    logger.info(
        "[brain] legacy run_learning_cycle DISABLED via "
        "CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0. Phase 2 handlers run "
        "instead. Set =1 to re-enable for emergency rollback."
    )
    skip_cycle, skip_reason = True, "legacy_cycle_disabled"
else:
    skip_cycle, skip_reason = _should_skip_reconcile_pass(status)
```

The flag stays as the emergency rollback switch. If Phase 2 handlers
turn out to be insufficient and we need to fall back, flipping the
env var re-enables the cycle without a code change.

**No code deletion in this brief.** Just gate the invocation.

### Step 2 — Remove the cold-start carve-out

In `_should_skip_reconcile_pass`, change line 1054-1055:

```python
if _LAST_RECONCILE_PASS_AT is None:
    return False, "cold_start_first_cycle"
```

To:

```python
if _LAST_RECONCILE_PASS_AT is None:
    # Cold start: do NOT auto-trigger a cycle. The legacy cycle is
    # disabled by default; restarts no longer force a 60-140 minute
    # crash-prone reconcile pass. If the cycle is re-enabled via
    # CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1, the safety-floor elapsed
    # check below fires the first cycle naturally after MAX_INTERVAL_S.
    _LAST_RECONCILE_PASS_AT = time.time()  # initialize as if just ran
    return True, "cold_start_no_auto_trigger"
```

This is defensive even when the kill switch (Step 1) is already
gating: belt-and-suspenders so the cycle never runs as a side effect
of a cold start.

### Step 3 — Disable the safety floor by default

In the env default at line 1023:

```python
# OLD: 4-hour safety floor
_RECONCILE_PASS_MAX_INTERVAL_S = int(os.environ.get(
    "CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S", str(4 * 3600)
))

# NEW: effectively disabled (1 year). Operator can re-enable a
# meaningful safety floor by explicitly setting the env var.
_RECONCILE_PASS_MAX_INTERVAL_S = int(os.environ.get(
    "CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S", str(365 * 24 * 3600)
))
```

Comment the new value with: "Default disabled. The legacy cycle is
gated off via CHILI_BRAIN_LEGACY_CYCLE_ENABLED. This safety-floor
default of 1 year is effectively never. If the operator re-enables
the cycle, set CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S to a real value
like 14400 (4h)."

### Step 4 — Survey event-handler coverage

This step produces the Phase 2 backlog document. It's pure read-only
analysis; output is a Markdown table.

Audit `scripts/brain_worker.py` and `app/services/trading/` for:

1. **`brain_work_event` types currently emitted** (search for
   `brain_work_event` insertions, look at the `event_type` column
   distribution in DB).
2. **Handler functions that consume them** (search for the dispatch
   loop and the handler registry).
3. **Step types in `run_learning_cycle` that are NOT yet covered**
   by any handler.

Output the table at `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md`:

```markdown
# Phase 2 Handler Migration Backlog

Inventory of legacy `run_learning_cycle` steps and their event-handler
coverage status. Generated by f-kill-legacy-learning-cycle.

| Step / Work-type | Legacy location | Handler status | Impact while uncovered |
|---|---|---|---|
| mine | learning.py:... | ✅ shipped FIX 36 | none — covered |
| cpcv_gate | learning.py:... | ⏸ pending | promotion gate stops re-evaluating |
| promote | learning.py:... | ⏸ pending | new patterns can't auto-promote |
| demote | learning.py:... | ⏸ pending | failing patterns aren't auto-demoted |
| regime_ledger | learning.py:... | ⏸ pending | regime classification stale |
| update_pattern_stats_from_closed_trades | learning.py:4798 | ⏸ pending | f-evidence-canonical-writer can't fire |
| ... | ... | ... | ... |
```

The executor populates this from the actual codebase. Each row's
"Impact while uncovered" is the operator-facing description of what
breaks until the handler ships.

### Step 5 — Smoke verification

After deploy:

1. Restart brain-worker: `docker compose restart brain-worker`
2. Watch logs for 30 minutes:
   ```powershell
   docker compose logs brain-worker --since 30m | Select-String "run_learning_cycle|reconcile_pass_completed|legacy_cycle_disabled"
   ```
   Expected: `legacy_cycle_disabled` line on every loop iteration;
   zero `learning_cycle_end` events; zero
   `psycopg2.OperationalError: server closed the connection` events.
3. SQL probe:
   ```sql
   SELECT COUNT(*) FROM pg_stat_activity
    WHERE application_name = 'chili-brain-worker'
      AND state = 'idle in transaction'
      AND state_change < NOW() - INTERVAL '5 minutes';
   ```
   Expected: 0 (no long idle-in-tx holds because no long-running
   cycle).
4. Brain-worker memory + CPU:
   ```powershell
   docker stats --no-stream chili-home-copilot-brain-worker-1
   ```
   Expected: dramatically lower CPU and memory than during cycle
   runs. Steady-state should be under 1 GiB.
5. Confirm event-driven path still functioning:
   - mining still happens (handler #1 fires on new candidate
     observations)
   - fast_backtest queue still drains (FIX 34's independent loop)
   - broker-sync continues (separate worker)

## Constraints / do not touch

- **Default mode stays paper.** No live placement enable.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **Do not delete `run_learning_cycle` source code.** This brief
  disables invocation only. The function stays callable for
  emergency rollback via `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1`.
- **Do not delete `update_pattern_stats_from_closed_trades`.** It's
  the canonical-aware writer just shipped; it'll be re-wired into
  an event handler in `f-handler-pattern-stats` (Phase 2 brief).
- **Do not modify event handler #1 (mine / FIX 36).** It works.
- **Do not modify FIX 34's independent fast_backtest loop.** It works.
- **Do not modify scheduler-worker's APScheduler jobs.** They are
  already event/cron-driven, not part of `run_learning_cycle`.
- **No threshold tuning.** This brief disables a path; doesn't tune
  thresholds.
- **No migrations.** Pure config + 30 lines of brain_worker.py.
- **No `git push --force`.** PROTOCOL Hard Rule 4.
- **The PHASE2_HANDLER_BACKLOG.md should not list speculative work.**
  Only document steps that ACTUALLY exist in `run_learning_cycle`
  today, with their concrete location.

## Out of scope

- Shipping any handler. Phase 2 briefs.
- Deleting `run_learning_cycle` source. Final cleanup brief.
- DB connection pool / TCP keepalive config changes. The cycle
  disable removes the long-idle pattern that triggers the drops; if
  other long-running queries surface drops, separate brief.
- Pattern-evidence backfill via the now-disabled cycle path. The
  data we already have is what we have until f-handler-pattern-stats
  ships.
- Re-running pattern-aware backtests. Separate concern.
- LLM-context (`position_plan_generator`) pattern-evidence path.
- Live-mode partials. Still queued separately.

## Success criteria

1. **Brain-worker runs 30+ minutes post-deploy with zero
   `learning_cycle_end` events** in its logs. Verified via the
   smoke step.
2. **Zero `psycopg2.OperationalError: server closed the connection`
   events** in brain-worker logs over the 30-min window. The
   absence of long-running cycles eliminates the trigger.
3. **`legacy_cycle_disabled` log line emitted on every loop
   iteration**, confirming the kill switch is active.
4. **Idle-in-transaction holds drop to zero** for the brain-worker
   application_name (within 5 minutes post-deploy).
5. **Brain-worker memory + CPU drop materially.** Steady-state
   under 1 GiB and under 50% of one core.
6. **`docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` exists** with at
   least 5 rows (one per known-pending handler) and accurate
   `Impact while uncovered` text per row.
7. **Existing event-driven paths still working post-deploy**:
   - Handler #1 (mine) firing on new patterns (verify via
     brain_work_events DB query)
   - FIX 34 fast_backtest loop draining queue
   - Broker-sync still running
8. **CC report** at
   `docs/STRATEGY/CC_REPORTS/<date>_f-kill-legacy-learning-cycle.md`
   per PROTOCOL format. Include the post-deploy memory + log
   snapshot inline as the verification artifact.

## Rollback plan

- **Emergency rollback (no code change)**: set
  `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1` in the brain-worker env (or
  docker-compose.yml), restart brain-worker. The legacy cycle path
  re-engages exactly as before. Use this if Phase 2 handler gaps
  turn out to break something operator-critical.
- **Partial rollback (re-enable safety floor)**: set
  `CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S=14400` to bring the 4h
  floor back. Combined with the kill switch, this lets you
  schedule occasional cycles while keeping cold-start free.
- **Full code rollback**: `git revert` the implementation commit.
  Behavior reverts to "cycle on every cold start + every 4h."
- **No data loss** — this brief touches no schema, no row data, no
  migrations.

## Open questions for Cowork (surface in CC report only if relevant)

1. **The PHASE2_HANDLER_BACKLOG inventory** — surface the actual
   uncovered step list and per-step impact. Some steps may be
   harmless to skip (e.g., dead-ticker decay; runs once a day,
   minor); others are critical (e.g., promote/demote; live
   patterns get stale). Surface the prioritization for Phase 2
   sequencing.

2. **`update_pattern_stats_from_closed_trades` impact** —
   confirm in the backlog that pattern evidence will be frozen
   until `f-handler-pattern-stats` ships. The data we have is the
   data we have. This is a known trade-off the algo-trader
   framing accepts: temporary evidence-staleness is preferable
   to a brain-worker that crashes 100% of the time.

3. **Other long-running queries** — once the cycle is disabled,
   are there any OTHER processes still holding 60+ minute
   transactions? If yes (probably the
   `momentum_symbol_viability` queries flagged in earlier
   diagnostics), they may also need the same DB-stability
   treatment. Surface in the backlog.

4. **Phase 2 sequencing recommendation** — based on the backlog,
   which handler is most operator-impactful to ship first?
   Likely candidates: (a) `f-handler-pattern-stats` (because
   today's evidence-canonical-writer depends on it), or (b)
   `f-handler-cpcv-gate + f-handler-demote` (because pattern
   lifecycle staleness has live trading implications). Surface
   recommendation; the operator decides.

5. **The 1-year safety-floor default** — that's effectively
   "off." If the operator wants a meaningful safety floor (e.g.,
   24h) once Phase 2 handlers ship, they can set the env var.
   For now, defaulting to "off" is correct because the cycle
   itself is off.
