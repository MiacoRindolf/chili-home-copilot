# NEXT_TASK: f-overnight-cleanup

STATUS: DONE

## Goal

Six discrete fixes / diagnostics, executed sequentially in one session.
Operator is going offline; CC runs through all phases autonomously,
commits per phase, ships ONE combined CC report at the end. Each phase
is independent — if one blocks, surface honestly and skip to the next.
**Goal is forward progress on as many phases as possible, not perfect
completion of all six.**

After this lands, the brain has:
- Startup verification that handlers actually load (prevents another
  6-day silent regression of the import-bug class)
- A connection-pool leak closed at its source (the
  `momentum_symbol_viability` viability check)
- A clear diagnosis of why paper-trading has zero rows ever
- Live-trade close events emitting from all 5 close-sites (currently
  only 1 of 5)
- Backtest completion events emitting from FIX 34's independent loop
- DB watchdog actually killing long idle-in-tx (currently it warns
  but doesn't kill — paper tiger)

## Why now

You're going out and want forward progress on the architectural
follow-ups today's deep diagnostic surfaced. Each phase is small and
independent. Running them in sequence as one CC session preserves
algo-trader-architect priority order without stalling on operator
approval between briefs.

## Phase ordering and stop-on-blocker policy

**Sequence (smallest → largest impact):**
1. `f-handler-load-verification` (~30 min, hygiene)
2. `f-fix-momentum-viability-tx-leak` (~45 min, leak fix)
3. `f-diagnose-paper-runner-output-gap` (~60 min, pure diagnostic)
4. `f-fix-live-trade-closed-emitter` (~90 min, multi-site fix)
5. `f-fix-backtest-completed-emitter` (~45 min, single-site fix)
6. `f-fix-db-watchdog-kill-action` (~30 min, fix-or-confirm)

**Stop-on-blocker policy:** If a phase has a hard blocker (e.g., needs
operator authorization, encounters a frozen contract, requires a
schema change that wasn't anticipated), commit progress, surface the
blocker in the CC report's per-phase section, **skip to the next
phase**. Don't deadlock waiting on operator input.

**Commit boundaries:** ONE commit per phase. Each commit message
references the phase slug (`<phase>-<short-summary>`). If a phase
ships nothing, no commit; just an entry in the CC report.

**Migrations**: assume each phase can claim the next sequential
migration ID at execution time. Run
`scripts/verify-migration-ids.ps1` before each migration commit.

---

# Phase 1 — f-handler-load-verification

## Goal

Add a startup-time check that imports all 6 brain_work handler modules
and asserts each has its `handle_*` callable. Logs `[handler_verify]
OK 6/6` on success or fails loud on FAIL with a clear list of which
handlers are broken. Prevents the kind of 6-day silent
`ModuleNotFoundError` regression that today's
`f-handler-pattern-stats` brief uncovered.

## Source material

- `scripts/brain_worker.py` — the worker entry point. Verification
  runs at startup, before the main loop.
- `app/services/trading/brain_work/handlers/{mine, cpcv_gate, promote,
  demote, regime_ledger, pattern_stats}.py` — the 6 handlers.
- `app/services/trading/brain_work/dispatcher.py:272-321` — the
  dispatch branches that call each handler's `handle_*`.

## Path

In `scripts/brain_worker.py`, near the top of `main()` after logger
setup but before any work loop starts, add:

```python
def _verify_handler_modules() -> None:
    """Startup verification: every handler module must import cleanly
    and expose at least one `handle_*` callable.

    Failed handlers crash brain-worker on startup with a clear
    multi-line error. Better than a 6-day silent ModuleNotFoundError
    regression where the dispatcher's try/except swallows the failure.
    """
    import importlib
    expected = {
        "app.services.trading.brain_work.handlers.mine":
            ["handle_market_snapshots_batch"],
        "app.services.trading.brain_work.handlers.cpcv_gate":
            ["handle_backtest_completed"],
        "app.services.trading.brain_work.handlers.promote":
            ["handle_pattern_eligible_promotion"],
        "app.services.trading.brain_work.handlers.demote":
            ["handle_paper_trade_closed",
             "handle_live_trade_closed",
             "handle_broker_fill_closed"],
        "app.services.trading.brain_work.handlers.regime_ledger":
            ["handle_paper_trade_closed",
             "handle_live_trade_closed",
             "handle_broker_fill_closed"],
        "app.services.trading.brain_work.handlers.pattern_stats":
            ["handle_paper_trade_closed",
             "handle_live_trade_closed",
             "handle_broker_fill_closed"],
    }
    failures: list[str] = []
    for mod_name, callables in expected.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            failures.append(f"  IMPORT-FAIL {mod_name}: {type(e).__name__}: {e}")
            continue
        for callable_name in callables:
            if not callable(getattr(mod, callable_name, None)):
                failures.append(f"  MISSING-CALLABLE {mod_name}.{callable_name}")
    if failures:
        msg = (
            "[handler_verify] STARTUP CHECK FAILED — brain-worker "
            "would dispatch to broken handlers. Fix before re-running.\n"
            + "\n".join(failures)
        )
        logger.error(msg)
        raise SystemExit(msg)
    logger.info(
        "[handler_verify] OK 6/6 handlers loaded cleanly: %s",
        ", ".join(sorted(m.rsplit(".", 1)[1] for m in expected)),
    )
```

Call `_verify_handler_modules()` from `main()` before the worker enters
its loop. Failure = `SystemExit`, so brain-worker dies fast with the
operator-readable error.

Verify the call is gated so it doesn't run during pytest (check
existing patterns; `os.environ.get("CHILI_PYTEST")` per CLAUDE.md).

## Tests

`tests/test_handler_load_verification.py`:

1. ✅ Happy path: all 6 modules import + all expected callables
   present → no failures.
2. ✅ Synthetic missing-callable: monkeypatch one module to lack a
   callable → `SystemExit` with that callable named in the error.
3. ✅ Synthetic import error: monkeypatch one module's spec to raise
   ImportError on import → `SystemExit` with "IMPORT-FAIL" prefix.
4. ✅ Pytest gating: when `CHILI_PYTEST=1`, the function is a no-op
   (or skipped at the call site) so unit tests don't trigger.

## Success criteria

- `_verify_handler_modules()` exists in `scripts/brain_worker.py`.
- Called from `main()` before work loop.
- 4/4 tests pass.
- Smoke: brain-worker starts cleanly, log shows
  `[handler_verify] OK 6/6 handlers loaded cleanly: ...`.

## Commit message

`fix(brain-worker): handler-load verification on startup (f-handler-load-verification)`

---

# Phase 2 — f-fix-momentum-viability-tx-leak

## Goal

Close the connection-pool leak that's been spawning 9-12 simultaneous
idle-in-tx sessions on the `momentum_symbol_viability` query for at
least 12 hours (per today's deep diagnostic). Sessions held times
growing past 600s without commit. Same SQLAlchemy session-lifecycle
bug class as f-leak-3 (yfinance Thread leak from 2026-05-04).

## Source material

- `app/services/trading/momentum_symbol_viability.py` (or wherever the
  viability check lives — locate via grep).
- The query pattern surfaced in today's deep diagnostic A2b:
  `SELECT momentum_symbol_viability.id AS momentum_symbol_viability_id, ...`
- Existing pattern of session lifecycle in
  `app/services/trading/brain_work/handlers/demote.py` (correct usage).
- Memory: `reference_fleak3_yf_thread_leak_fix.md` (similar lifecycle
  pattern fix).

## Path

1. Grep for `momentum_symbol_viability` to find the read site(s).
2. Identify the function that runs the SELECT and trace its session
   lifecycle. Most likely cause:
   - Function uses a passed-in or globally-cached session
   - Runs the SELECT, returns the result
   - Caller never commits/closes; session sits in idle-in-tx
3. Fix:
   - **Preferred**: replace bare `session.execute()` with
     `with SessionLocal() as session: ... ; session.commit()` block.
     Self-contained; transaction always closes.
   - **Alternate**: if the function is intentionally short-lived,
     ensure caller wraps in `try/finally: session.close()`.
4. **Do NOT** add new transactions or change semantics. The function
   is a pure read; commit-on-exit is correctness, not behavior change.
5. Verify: post-fix, the query should appear in `pg_stat_activity`
   only briefly; no idle-in-tx accumulation.

## Tests

`tests/test_momentum_viability_tx_lifecycle.py`:

1. ✅ Calling the viability function does not leave an open
   transaction afterwards. Probe via
   `db.connection().info.get("transaction_started")` or equivalent
   SQLAlchemy introspection.
2. ✅ The viability function returns the same data post-fix as
   pre-fix (regression guard against accidental semantic change).
3. ✅ Multiple concurrent calls produce zero accumulated idle-in-tx
   sessions over a 30-second window (synthetic, with mocked DB).

## Success criteria

- Viability function uses a self-contained transaction.
- Tests pass.
- Smoke: post-deploy, query
  `SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'
  AND query LIKE '%momentum_symbol_viability%'` returns 0 within 5
  minutes of a viability check.

## Commit message

`fix(momentum-viability): close session-lifecycle leak (f-fix-momentum-viability-tx-leak)`

---

# Phase 3 — f-diagnose-paper-runner-output-gap

## Goal

Pure diagnostic. Find why `trading_paper_trades` has 0 rows total
despite `CHILI_MOMENTUM_PAPER_RUNNER_ENABLED=1` and the paper-runner
job firing every minute (`Momentum paper runner: ticked 2 session(s)`).

**No code fix in this phase.** Output is a written analysis at
`docs/AUDITS/<date>_paper-runner-output-gap.md` plus an updated entry
in `PHASE2_HANDLER_BACKLOG.md` referencing the eventual fix brief.

## Source material

- `app/services/trading_scheduler.py` — find the
  `momentum_paper_runner_batch` APScheduler job definition.
- The job calls something that's supposed to insert into
  `trading_paper_trades`. Trace the chain.
- `app/services/trading/momentum/` (or wherever the paper-runner
  logic lives — locate via grep on `momentum_paper_runner`).

## Path

The diagnostic should answer these questions in order:

1. **What does the paper-runner-batch job actually call?** Read the
   job definition. Is it calling a function that's supposed to insert
   trades, or only updating session state?

2. **What does "ticked 2 session(s)" mean?** Find the log line in
   the source. Sessions of what — paper-runner sessions? Are these
   distinct from `trading_paper_trades`?

3. **Is there a separate "paper sessions" concept in the schema?**
   Check for tables like `momentum_paper_sessions`,
   `paper_run_sessions`, etc. Is paper-runner inserting there
   instead of into `trading_paper_trades`?

4. **Is paper-runner gated on something that's currently false?**
   E.g., requires a "live session" object that's never created;
   requires user_id matching that doesn't exist; requires market
   hours / weekend that's blocking.

5. **Were there ever paper trades in this DB historically?** Check
   `trading_paper_trades` count (we know it's 0 now). Check git log
   on the paper-runner code for when it last changed semantically —
   maybe the wiring was broken at some specific commit.

6. **What's the relationship between "paper-runner ticked N
   sessions" and `trading_paper_trades` row creation?** Map the
   call graph. Is there a missing INSERT site? A swallowed exception?
   A silent skip?

Each question's answer goes in the output doc with file:line citations
and supporting query results.

## Output deliverable

`docs/AUDITS/<date>_paper-runner-output-gap.md` containing:

- Executive summary: 2-3 paragraph algo-trader-architect read on what's
  broken
- Per-question section with finding + supporting evidence
- Root cause (or "still unknown, here's what to investigate next")
- Suggested fix brief slug (e.g., `f-fix-paper-runner-X`) if root
  cause identified

Plus update `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` to reference the
audit doc + queued fix brief.

## Tests

No new tests. Pure analysis.

## Success criteria

- Audit doc exists with executive summary + 6 question sections + root
  cause statement.
- `PHASE2_HANDLER_BACKLOG.md` references the audit doc.
- If root cause IS identified, a follow-up brief slug is named.

## Commit message

`docs(audit): paper-runner output gap diagnostic (f-diagnose-paper-runner-output-gap)`

---

# Phase 4 — f-fix-live-trade-closed-emitter

## Goal

Close the live-trade-close emitter coverage gap surfaced in today's
`f-handler-pattern-stats` audit. Currently `on_live_trade_closed` is
called from exactly one site (`portfolio.py:185`); 4 of 5 close-sites
bypass the emitter entirely:

- `app/services/trading/stop_engine.py:1057` (stop hits)
- `app/services/trading/robinhood_exit_execution.py:394` (broker exit fills)
- `app/services/trading/emergency_liquidation.py:104` (emergency liquidations)
- broker_sync (find the close-site in `broker_service.py` or wherever
  it sets `Trade.status='closed'`)

After this fix, every live-trade close emits `live_trade_closed` event,
which feeds demote / regime_ledger / pattern_stats handlers.

## Source material

- `app/services/trading/brain_work/execution_hooks.py:on_live_trade_closed`
  — the function to call. Already implemented.
- `app/services/trading/portfolio.py:185` — the existing call site.
  Use as model for the call shape.
- The 4 bypass sites listed above.

## Path

For each of the 4 bypass sites:

1. Locate the exact line where `Trade.status='closed'` is set (or
   where the close transaction is committed).
2. Add `on_live_trade_closed(db, trade)` immediately AFTER the close
   in the same transaction (mirror of `_paper_close_ledger` pattern
   in `paper_trading.py:240-245`).
3. Wrap in try/except so emitter failure can't break the close
   transaction:
   ```python
   try:
       from .brain_work.execution_hooks import on_live_trade_closed
       on_live_trade_closed(db, trade)
   except Exception:
       logger.debug("[exec_hooks] on_live_trade_closed failed", exc_info=True)
   ```
4. Repeat for all 4 sites.

For broker_sync specifically: find the actual close site, not just
where status is read. Likely in `app/services/broker_service.py`'s
sync path. May need to grep for `Trade.status='closed'` or
`trade.status='closed'` to find all assignment sites.

## Tests

`tests/test_live_trade_close_emitter_coverage.py`:

1. ✅ Stop-engine close path: synthetic stop hit on a Trade →
   `brain_work_events` has a new `live_trade_closed` row for that
   trade.
2. ✅ Robinhood exit-execution path: same shape.
3. ✅ Emergency liquidation path: same shape.
4. ✅ Broker-sync close path: same shape.
5. ✅ Existing portfolio.py path still works (regression guard).
6. ✅ Emitter failure does not break the close transaction (mock
   the emitter to raise; trade still closes).

## Success criteria

- 4 new call sites added, all wrapped in try/except.
- 6/6 tests pass.
- Smoke: post-deploy, a live trade closing via any of the 4 bypass
  paths produces a `live_trade_closed` event in
  `brain_work_events`.

## Commit message

`fix(live-trade-emitter): cover stop/exit/emergency/broker-sync close paths (f-fix-live-trade-closed-emitter)`

---

# Phase 5 — f-fix-backtest-completed-emitter

## Goal

When FIX 34's independent fast_backtest loop completes a backtest,
emit a `backtest_completed` event so `cpcv_gate.py` (handler #2) can
fire. Currently the independent loop bypasses the event path
entirely — backtests run, parity rows accumulate (45k+ over a few
hours), but `cpcv_gate.py` never gets called.

## Source material

- `scripts/brain_worker.py:_run_fast_backtest_independent_loop` (or
  similar name — locate via grep on FIX 34 / fast_backtest)
- `app/services/trading/brain_work/emitters.py:emit_backtest_completed_outcome`
  (or similar). Find the existing emitter for completion events.
- `app/services/trading/brain_work/handlers/cpcv_gate.py` — handler
  that subscribes to `backtest_completed`.

## Path

1. In the fast_backtest independent loop, find the completion site
   (where one backtest run finishes its bar loop and produces its
   summary row).
2. Add the emit call immediately after, in the same DB session:
   ```python
   from app.services.trading.brain_work.emitters import (
       emit_backtest_completed_outcome,
   )
   try:
       emit_backtest_completed_outcome(
           db,
           scan_pattern_id=int(pattern_id),
           # ... other required fields per the emitter's signature ...
       )
   except Exception:
       logger.warning("[fast_backtest] emit_backtest_completed failed",
                      exc_info=True)
   ```
3. Run `verify-migration-ids.ps1` (no migration in this phase, but
   habit).

If the emitter doesn't exist yet, create it in `emitters.py` with
the same shape as `emit_paper_trade_closed_outcome`. Match the
payload structure cpcv_gate's handler expects.

## Tests

`tests/test_backtest_completed_emitter.py`:

1. ✅ Synthetic backtest completion → `brain_work_events` has a
   `backtest_completed` row with the right pattern_id and payload.
2. ✅ Emitter failure does not break the backtest (the bar loop
   continues, summary row still written).
3. ✅ cpcv_gate handler can consume the emitted event end-to-end
   (integration test if feasible; else surface as deferred).

## Success criteria

- Emit call added at the completion site.
- 2-3 tests pass.
- Smoke: post-deploy, after one fast_backtest completes,
  `brain_work_events` has a `backtest_completed` row with `status=done`
  (after cpcv_gate processes it).

## Commit message

`fix(fast-backtest-emitter): emit backtest_completed events (f-fix-backtest-completed-emitter)`

---

# Phase 6 — f-fix-db-watchdog-kill-action

## Goal

Confirm or fix the `db_watchdog` kill behaviour. Today's diagnostic
showed db_watchdog warns at 120s, claims kill at 600s, but actual
held times grow to 624s, 921s, 482s, 542s — past the kill threshold
without being killed.

Two possibilities:
1. The kill action is implemented but failing silently (no
   `pg_terminate_backend()` permission, exception swallowed,
   wrong PID, etc.)
2. The kill action is not implemented at all (only warning is)

Read the source, determine which, ship the fix.

## Source material

- `app/services/db_watchdog.py` — locate first via grep.
- The warn-log lines: `[db_watchdog] idle-in-tx pid=N app=X held for Ys (warn threshold 120, kill at 600)`

## Path

1. Read `db_watchdog.py` end-to-end.
2. Find the kill code path. Is it implemented? Does it call
   `pg_terminate_backend()`?
3. If NOT implemented: add it. Use SQL like:
   ```sql
   SELECT pg_terminate_backend(:pid)
   ```
   guarded by:
   - `held_s > 600`
   - `application_name LIKE 'chili%'` (don't kill foreign apps)
   - `state = 'idle in transaction'` (don't kill active queries)
4. If implemented but silently failing: add `logger.error()` on the
   exception path so the failure is visible.
5. Either way: log clearly when a kill IS executed:
   ```
   [db_watchdog] KILLED idle-in-tx pid=N app=X held=Ys query=<excerpt>
   ```
6. Verify the watchdog's PG user has `pg_signal_backend` permission
   (or is a superuser). If not, this needs a postgres-side grant —
   surface as a separate operator-action item.

## Tests

`tests/test_db_watchdog_kill.py`:

1. ✅ Synthetic 700s held idle-in-tx session triggers a `KILLED`
   log line.
2. ✅ Kill respects the application_name filter (foreign apps not
   killed).
3. ✅ Kill respects the state filter (active queries not killed).
4. ✅ Permission failure (mock `pg_terminate_backend` to return
   FALSE) logs ERROR but doesn't crash the watchdog loop.

## Success criteria

- Kill action confirmed implemented (and fixed if broken).
- Tests pass.
- Smoke: post-deploy, observe logs for `[db_watchdog] KILLED` lines
  on the next viability-check leak (Phase 2 should also help).

## Commit message

`fix(db-watchdog): make kill action actually kill (f-fix-db-watchdog-kill-action)`

---

# Combined CC Report

After all phases complete (or are blocked), write ONE CC report at
`docs/STRATEGY/CC_REPORTS/<date>_f-overnight-cleanup.md` covering all
six phases. Structure:

```markdown
# CC_REPORT: f-overnight-cleanup

## What shipped (per phase)

### Phase 1 — f-handler-load-verification
- Status: SHIPPED / BLOCKED / SKIPPED
- Commit: <hash>
- Files: <count>
- Tests: <pass/total>
- Key finding: ...

### Phase 2 — f-fix-momentum-viability-tx-leak
- ... same shape ...

(repeat for all 6 phases)

## Cross-phase observations
(things that turned up in multiple phases or that affect cowork's mental model)

## Surprises / deviations
(per-phase or cross-cutting)

## Open questions for Cowork
(per-phase or cross-cutting)

## What needs operator action
(things only the operator can do — e.g., grant pg_signal_backend role)
```

## Constraints / do not touch (cross-phase)

- **Default mode stays paper.** No live-placement enable.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **Do not re-enable `run_learning_cycle`.** Stays gated off via
  `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0`.
- **Do not modify the canonical evaluator** (`exit_evaluator.py`).
- **Do not modify the realized-EV gate** (`realized_ev_gate.py`).
- **Do not modify any of the 5 existing Phase 2 handlers** unless
  the import-bug-class issue surfaces in another file (in which
  case fix it the same way as today's pattern_stats brief).
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration IDs**: each phase claims the next sequential at
  execution time. Verify with `verify-migration-ids.ps1`.
- **No `git push --force`.** PROTOCOL Hard Rule 4.
- **One commit per phase.** Atomic recoverability.
- **If a phase is blocked, commit progress so far + skip to next.**
  Don't deadlock waiting on operator input. Surface blocker in CC
  report.

## Out of scope (cross-phase)

- The `run_learning_cycle` source code deletion. Final cleanup brief.
- Other handlers from `PHASE2_HANDLER_BACKLOG.md` not listed above.
- DB-stability config (TCP keepalives, pool_pre_ping). The cycle
  disable + Phase 2 leak fix should obviate.
- Position-side timeframe column (Trade/PaperTrade.timeframe).
- LLM-context (`position_plan_generator`) pattern-evidence path.
- `f-cron-stale-promoted` (sweep-mode demote gap).
- Backtest-derived evidence correction.

## Success criteria (cross-phase)

1. **As many phases as possible are SHIPPED.** Target: 5/6. Acceptable
   floor: 3/6 if multiple hard blockers surface.
2. **Each shipped phase has its commit + tests pass.**
3. **Combined CC report covers all 6 phases honestly** (SHIPPED /
   BLOCKED / SKIPPED + reasoning).
4. **Operator-action items surfaced clearly** (e.g., postgres role
   grants needed).
5. **`PHASE2_HANDLER_BACKLOG.md` updated** with status per phase.
6. **Brain-worker still functional** post-each-commit (the load-
   verification from Phase 1 catches if any phase accidentally breaks
   a handler import).

## Rollback plan

- Per-phase: each is an independent commit. `git revert <phase-N-commit>`
  rolls back just that phase.
- Cross-phase: if multiple phases need rollback, revert in reverse
  order (Phase 6 first, Phase 1 last).
- No data rollback — phases don't introduce schema or data mutations
  that aren't already covered by individual rollback plans.
- `CHILI_BRAIN_LEGACY_CYCLE_ENABLED` stays `0` regardless of
  rollbacks.

## Open questions / what to surface in CC report

For each phase, the report should answer:

1. **Phase 1**: were any of the 6 handlers found to have OTHER bugs
   beyond import (e.g., missing callable, wrong signature)?
2. **Phase 2**: was the fix surgical (one function), or did the leak
   originate from multiple call sites? What's the actual lifetime
   pattern of the function?
3. **Phase 3**: ROOT CAUSE for paper-runner output gap. If unknown,
   what's the next step?
4. **Phase 4**: confirm 4 bypass sites were all real and accessible,
   not abstracted behind layers that complicated the fix.
5. **Phase 5**: did the emitter exist already, or did this phase
   create it? What was the missing payload field, if any?
6. **Phase 6**: was the watchdog's kill code missing entirely, or
   broken? Did the postgres-side `pg_signal_backend` permission
   need to be granted?

Cross-phase:
- Did any phase encounter a frozen contract that required Cowork
  authorization? Surface explicitly.
- Did any phase reveal another silent regression (like today's 6-day
  handler-import bug)? If yes, that's the next-priority Cowork
  follow-up.
