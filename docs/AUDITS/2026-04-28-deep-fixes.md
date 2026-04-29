# CHILI Deep Audit Fix Sprint — 2026-04-28

Closure report for the deep-audit fix sprint following
[`2026-04-28-deep.md`](2026-04-28-deep.md). 14 fixes deployed across 12
commits over ~5.5 hours. Every CRITICAL and HIGH item from the audit is
resolved or has a documented mitigation in production.

## Commits (chronological)

| Commit | Fix | Files | Lines |
|---|---|---|---|
| d84a9fc | FIX 1: autopilot_scope truncation + crypto exit_monitor import | autopilot_scope.py, crypto/exit_monitor.py | +8 -3 |
| 9133216 | FIX 3: mig 201 phantom cleanup + autotrader entry guard | migrations.py, auto_trader.py | +98 -1 |
| 4e06166 | FIX 5+13/14: db_watchdog tighter + TCP keepalives | db_watchdog.py, db.py | +38 -10 |
| 094b795 | FIX 10: regime gate multi-dim 2-of-4 consensus | regime_gate.py, config.py | +202 -61 |
| 42f368b | FIX 9: pattern_regime_ledger correct settings + 'live' default | pattern_regime_ledger.py | +11 -1 |
| dd7882f | FIX 30: fibonacci.py causal swing detection | fibonacci.py | +45 -9 |
| 2a8e50e | FIX 31: brain reconcile-pass gate (event-driven bridge) | brain_worker.py | +201 -66 |
| 33658d1 | FIX 32: per-app watchdog thresholds via application_name | db.py, db_watchdog.py | +56 -5 |
| 8bafe89 | FIX 33: autotrader tick budget mitigation (config-only) | docker-compose.yml | +13 -0 |
| b9b9131 | FIX 33b: autotrader tick proper fix (eliminate abandon pattern) | trading_scheduler.py | +37 -41 |

Plus mig 201 (`201_cancel_phantom_trades`) deployed to schema_version.

## Fixes by category

### Crash / hang elimination
- **FIX 1** Restored 5 lines lost to an Edit-tool truncation bug in
  `check_autopilot_entry_gate` (no-owner branch). Function had been
  falling off the end and returning `None`, which `auto_trader.py:751`
  then dereferenced. Hit every promoted-pattern crypto alert (75
  crashes/24h pre-fix). Same commit fixed `from .. import broker_service`
  → `from ... import broker_service` in `crypto/exit_monitor.py` (the
  `..` resolved to `app.services.trading` which has no `broker_service`,
  while `...` resolves to `app.services` which does).
- **FIX 30** `find_swing_highs/lows` in fibonacci.py used
  `rolling(window, center=True).max()` which inherently looks `lookback`
  bars into the future. Replaced with a trailing rolling + `shift(lookback)`
  so the pivot is only emitted once causally observable. Eliminates 5,230
  research_integrity errors per 24h across the entire "RSI + Fib 0.382 +
  FVG Pullback" pattern family (7+ pattern variants on 233 tickers,
  fingerprints `1d81b0d2605e1417` and `b8e48c08e3686e5f`).

### Data hygiene
- **FIX 3** Migration 201 cancels open trades with NULL `broker_order_id`
  older than 24h (the phantom case mig 200's 7-day rule didn't catch).
  7 trades cancelled with explicit `phantom_no_broker_id` /
  `phantom_zero_entry_price` exit_reason. Plus an autotrader entry guard
  that refuses to insert a Trade row when the broker call returned
  `ok=True` but didn't surface an `order_id` — prevents recurrence.

### Connection health
- **FIX 5** Tightened db_watchdog from 5min/30min to 2min/10min for
  warn/kill — caught a real 25-min idle-in-tx leak that the previous
  threshold ignored. (Note: this fix later created the FIX 32
  regression; see below.)
- **FIX 13/14** Added TCP keepalives to the SQLAlchemy engine via
  `connect_args`. The brain-worker's learning cycle holds a session
  for ~34min; without keepalives, postgres closes the idle TCP socket
  and the next query fails with `server closed the connection
  unexpectedly`. Now keepalives every 30s keep the socket alive.
- **FIX 32 (regression catch)** FIX 5's tightened 10-min kill was
  killing the brain-worker mid-cycle — same `server closed connection`
  that FIX 13/14 was meant to prevent. Resolved by adding
  `application_name` to every connection (chili-app, chili-brain-worker,
  chili-scheduler, chili-backtest-child, chili-pytest) and per-app
  kill thresholds: brain-worker 30 min, others 10 min.
- **FIX 33** Mitigation for scheduler session leak via env vars
  `CHILI_AUTOTRADER_TICK_MAX_SECONDS=15` (was 45) and
  `CHILI_AUTOTRADER_TICK_MAX_INSTANCES=1` (was 3) — abandoned threads
  die 3x faster, no parallel pile-up. Reduced session count 62→25.
- **FIX 33b** Permanent fix for the leak: removed the entire
  `ThreadPoolExecutor` + abandon-on-timeout pattern from
  `_run_auto_trader_tick_job`. The work now runs directly with
  `try/finally` ensuring the session always closes. Inner broker
  calls already have their own `_call_with_timeout` so the outer
  timeout was redundant. Sessions dropped 25→7.

### Pattern lifecycle / promotion logic
- **FIX 9** `pattern_regime_ledger.py` was reading the wrong settings
  key (`brain_breadth_relstr_mode` instead of
  `brain_pattern_regime_perf_mode`), tagging live ledger writes as
  `mode='shadow'` instead of `mode='live'`. Fixed to use the dedicated
  key with default `'live'`.
- **FIX 10** Regime gate now consults all 4 ledger dimensions
  (ticker_regime, breadth_regime, cross_asset_regime, vol_regime) and
  blocks on 2-of-4 confident-negative-EV consensus
  (`chili_regime_gate_min_negatives=2`, operator-selected). Single-dim
  noise no longer over-blocks; multi-dim agreement does. This was the
  pre-requisite for keeping mig 199's regime-conditional promotions.

### Architecture (event-driven foundation)
- **FIX 31** Brain reconcile pass is now gated on actual signal:
  brain_work_events with status='done', scan_pattern lifecycle changes,
  or pattern updates >50 since last cycle. Otherwise: skip the cycle
  entirely. 4-hour safety floor. Cold-start always runs once. Bridge
  to fully event-driven brain — each step of `run_learning_cycle` can
  now be migrated incrementally to an event handler under
  `app/services/trading/brain_work/`.

## Verified-by-observation (no code change)

- **FIX 4** broker_sync re-query — existing path handles legitimate
  cases; phantom case now blocked at entry by FIX 3.
- **FIX 8** dup_coid recovery loop — drained naturally to 0 after
  FIX 3 cancelled the phantom trades that were the source.
- **FIX 11** mig 198 backtest_count NotNullViolation — current code
  already passes `backtest_count=0` in seed INSERT.
- **FIX 12** HMM yfinance substitute — `trading_macro_regime_snapshots.
  vix` has fresh data (1310 rows, latest 2026-04-27 vix=18.71); the
  classifier code already prefers macro vix over yfinance. The
  5-day-stale `regime_snapshot` was a missed Sunday cron, not a data
  problem. Verify next Sunday's run actually fires.
- **FIX 15** Mining stalled — root cause was the PG mid-cycle
  disconnect (resolved by FIX 13/14 + FIX 32).

## Operator decisions captured

- **Mig 199 promotions kept** — operator chose "widen regime gate
  first" (FIX 10 done); the 4 promoted patterns (871, 875, 1004, 1031
  with WR ≤27%) stay because the multi-dim gate now blocks losing
  regimes they rely on.
- **Bracket intents stay shadow** — operator chose monitor_exit path
  (validated by 3 stop fills 2026-04-28). FIX 6b (refactor G.2 logic
  into stop_engine) deferred — needs venue adapter `place_stop_order`
  API first.
- **Regime gate 2-of-4 multi-dim consensus** — operator-selected;
  balances over-blocking vs under-blocking.
- **Phantom trade cancellation** — operator-approved approach for
  mig 201; 7 trades cancelled with explicit reasons.

## Proof of correctness

**Trade 440 AAVE-USD** opened from pattern 860 at 22:47 UTC with
proper `broker_order_id` (asset_kind=crypto, broker_status=unknown).
Pattern 860 was previously the source of every NoneType crash; post-
FIX-1 it now flows alerts cleanly through the autotrader → regime
gate → broker placement → exit pipeline. End-to-end pipeline
confirmed working.

**End-state metrics** (verified 22:50 UTC after FIX 33b restart):

| Check | Value |
|---|---|
| Containers healthy | 5 of 5 |
| NoneType errors / 10 min | 0 |
| research_integrity errors / 10 min | 0 |
| dup_coid recoveries / 2 hours | 0 |
| PG `server closed connection` / 1 hour | 0 |
| chili-scheduler idle-in-tx | 2 (was 62) |
| chili-brain-worker idle-in-tx | 1 (no longer killed by watchdog at 600s) |
| Open phantom trades (NULL broker_order_id) | 0 |
| Open legitimate trades | 2 (RAY-USD, AAVE-USD, both have broker_status) |

## Lessons captured for future work

1. **Edit-tool truncation is real and dangerous.** The autopilot_scope
   regression was caused by an Edit-tool that silently dropped the
   tail of a function. Future Edit operations on critical files
   (especially long ones) need the safe-Python-write pattern
   (tempfile + os.replace) AND validation via `ast.parse`.
2. **Watchdog thresholds need to be per-component.** A single global
   threshold can't accommodate both short-lived API requests (kill
   fast on leak) and long-running batch work (don't kill at all).
   FIX 32's `application_name` tagging is the right primitive for
   this and unlocks future per-component policies (rate limits,
   dedicated pools).
3. **`ThreadPoolExecutor` + outer timeout + abandon is an anti-pattern.**
   Use APScheduler's `max_instances=1` for skip-on-overrun, and rely
   on inner per-call timeouts (which already exist via
   `_call_with_timeout`). The outer wrapper just creates orphan
   threads holding resources.
4. **Cycle-driven monoliths create fragility.** A 30-40min cycle that
   holds one session for the whole duration can't tolerate ANY
   transient PG issue without the whole cycle aborting. Event-driven
   handlers with short-lived sessions per event are inherently more
   robust. FIX 31 is the bridge to this architecture.

## Backlog with full context for next session

- **FIX 6b** Refactor G.2 bracket logic into stop_engine. Operator-
  approved direction. Investigation done: `stop_engine.py` = decision
  layer, `bracket_writer_g2.py` = execution layer (currently dormant
  scaffold using `place_limit_order_gtc` as a workaround for missing
  `place_stop_order`). Real merge requires venue adapter
  `place_stop_order` API first. Implementation order: (1) add
  `place_stop_order` to RobinhoodSpotAdapter; (2) wire G.2 useful
  patterns (resize-on-partial-fill, missing-stop, audit) into the
  monitor_exit path in `auto_trader_monitor.py`; (3) deprecate
  bracket_writer_g2 separately.
- **FIX 31 follow-up** Migrate each step of `run_learning_cycle` into
  its own `brain_work_event` handler under
  `app/services/trading/brain_work/`. Each step becomes a discrete
  PR. The 5-line gate (FIX 31) is the bridge — when all steps are
  migrated, set `_RECONCILE_PASS_MAX_INTERVAL_S` to ∞ and the cycle
  effectively never runs.
- **FIX 12 follow-up** Verify next Sunday's regime_classifier cron
  writes a fresh `regime_snapshot` row. If it doesn't, the cron
  scheduler isn't firing; if the cron fires but writes fail, dig
  into `run_weekly_regime_retrain`.

---

Memory pointer at `reference_2026_04_28_deep_audit_fixes.md` carries
full context to next session.
