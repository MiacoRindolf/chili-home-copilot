# CC_REPORT: idle-in-transaction-hygiene

**Date:** 2026-06-07
**Type:** Operator-directed reliability fix (not a Cowork `NEXT_TASK` item; the queued
`f-position-identity-phase-5i-post-rename-soak` remains `STATUS: PENDING` and untouched).

## Context

The operator supplied a confirmed root-cause diagnosis of the system-wide Postgres
`OperationalError` → `PendingRollbackError` → `DetachedInstanceError` cascade
(seen across `chili-brain-worker`, autotrader, backtest workers, and the scheduler's
`pattern_position_monitor`).

**Root cause:** `app/db.py` injects `idle_in_transaction_session_timeout` per connection
(`database_idle_in_transaction_timeout_ms`, default **120 000 ms**). Long-running workers
held a transaction OPEN+IDLE across heavy non-DB compute / network I/O; once idle-in-
transaction exceeded 120s, Postgres terminated the connection (`FATAL: terminating
connection due to idle-in-transaction timeout`), the next ORM use raised `OperationalError`,
and the session poisoned. The FIX 13+14 TCP keepalives keep the *socket* alive but do
nothing against this application-level timer.

**Chosen approach (operator):** transaction **hygiene** (release/commit before long
compute) — *not* timeout-widening or a dedicated worker engine. No new config knobs, no
lock-holding change, no magic numbers. Each fix mirrors the existing
`_release_queue_parent_session()` idiom in `learning.py`.

## What shipped

| PR | Squash | Subsystem | Fix |
|----|--------|-----------|-----|
| [#488](https://github.com/MiacoRindolf/chili-home-copilot/pull/488) | `b16a03d` | backtest queue worker | `smart_backtest_insight._persist_result`: the read-only param-set lineage lookup `db.get(BacktestParamSet, …)` ran **after** `save_backtest()` committed, opening a fresh txn that sat idle across the next ticker's `run_pattern_backtest()` compute (the observed `trading_backtest_param_sets` idle). Fix = `db.rollback()` at the read-only tail. |
| [#490](https://github.com/MiacoRindolf/chili-home-copilot/pull/490) | `0adb42a` | backtest queue worker | Same function, pre-dispatch window: setup SELECTs (`_find_linked_pattern`, `ScanPattern` lookup, `_select_tickers`) left a txn open across **ticker 1's** compute. Fix = `db.rollback()` before the dispatch loop (setup verified read-only on `db`; cost derivation uses its own `_cost_db`). |
| [#492](https://github.com/MiacoRindolf/chili-home-copilot/pull/492) | `945d392` | `pattern_position_monitor` | One read txn (open-trades SELECT + broker-truth stale reconcile) was held across the per-trade loop while each trade did broker-quote + LLM I/O, with a single end-commit. Fix = commit before the loop + commit per trade. |

**Files touched:** `app/services/trading/backtest_engine.py`,
`app/services/trading/pattern_position_monitor.py`,
`tests/test_backtest_engine_queue_progress.py`. **Migrations added:** none.

## Verification

- `test_backtest_engine_queue_progress.py` + `test_backtest_smart_deadline.py` +
  `test_backtest_param_sets.py` — **11 passed** (DB-backed; real `save_backtest` commit
  path + soft-deadline partial-persist preserved). Regression assertion tightened to
  `rollbacks >= target_tickers + 1` (one pre-dispatch release + one per persisted ticker).
- `test_session_rollback_on_disconnect.py` (the #487 poison-recovery suite) +
  `test_pattern_monitor_health.py` + `test_pattern_position_monitor_decision_source.py`
  — **33 passed**. Confirms the monitor change preserves the #487 `rollback_if_poisoned`
  recovery on both error paths.
- No order-placement / safety-belt logic touched — only DB transaction granularity.

## Surprises / deviations

- **Diagnosis refinement (backtest):** the offending `trading_backtest_param_sets` idle is
  *not* the accumulated writes (`save_backtest` and `persist_rows_from_backtest_result`
  already commit). It is a **read-only** lineage SELECT firing after that commit. So the fix
  is a `rollback()` (release), not a new commit — discards nothing.
- **Parallel-codex git race:** a parallel codex agent shares this working tree + HEAD. During
  a 128s background test it switched the checkout to its own branch, so the monitor commit
  initially landed on codex's branch and the pushed branch was contaminated with codex's
  commit. Recovered by rebuilding the branch cleanly on `origin/main` via plumbing
  (`read-tree` + `update-index` + `commit-tree`, no checkout) and force-pushing; codex's work
  merged independently as **#491**, so the orphaned local commit harms nothing. The CC_REPORT
  itself was written in an **isolated `git worktree`** to avoid a repeat. (Memory
  `feedback_sync_before_change` updated accordingly.)

## Deferred — flagged, intentionally not changed

- **`evolve_pattern_strategies`** (learning.py): holds a read txn across a per-pattern loop,
  but the compute there is **in-memory** (Sharpe / fitness), not network — low risk of a
  >120s idle window. Touching the brain's pattern-evolution logic speculatively isn't worth
  the regression risk. `mine_patterns`, `validate_and_evolve`, and
  `run_promoted_pattern_fast_eval` were confirmed **safe** (they release/fetch before heavy
  work). `run_learning_cycle` commits per step (`_commit_step`), so windows are per-step, not
  the full ~34-min cycle.
- **`statement_timeout` sizing:** the `migrations.py:~20168` timeout applies **only** to the
  divergence index-creation migration (10s, env-configurable). Backtest/CPCV SELECTs have
  **no** `statement_timeout` binding — so the "canceling statement due to statement timeout"
  symptom is **not** coming from a backtest/CPCV timeout (the diagnosis's attribution was
  imprecise). The other `statement_timeout`s (autotrader candidate-select, divergence
  discovery) are already settings-bound/derived. Sizing anything here without the actual
  canceled-query text from the PG log would be an evidence-free magic number — flagged for
  operator confirmation instead.

## Open questions for Cowork

1. Do you want `evolve_pattern_strategies` hardened proactively, or left until PG logs show it
   actually trips the 120s idle kill?
2. For the `statement_timeout` cancellations: can you pull the canceled-query text from the PG
   log so we can bind a derived timeout to the right query (vs. guessing a value)?
3. Should this hygiene series get a Cowork review entry, given it bypassed the `NEXT_TASK`
   loop (operator-directed in-chat)?
