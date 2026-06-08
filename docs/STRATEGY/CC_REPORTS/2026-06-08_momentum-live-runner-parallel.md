# CC_REPORT: momentum-live-runner-parallel

**Date:** 2026-06-08
**Initiative:** Ross momentum lane (`project_momentum_lane`) — operational throughput, not strategy.
**Note:** This was an operator-direct task, not the standing `NEXT_TASK.md` (which is the unrelated position-identity Phase 5I soak — left untouched).

## What shipped

- **Commit / PR:** `0e51cd4` → squash-merged to `main` as **`4fa217a`** ([PR #522](https://github.com/MiacoRindolf/chili-home-copilot/pull/522)).
- **One-line:** Parallelize the `momentum_live_runner_batch` per-session loop with a small bounded thread pool so the batch fits inside its 30s APScheduler cadence.
- **Files (3):** `app/services/trading_scheduler.py` (new `_dispatch_live_runner_ticks` + rewritten `_run_momentum_live_runner_batch_job`), `app/config.py` (new `chili_momentum_live_runner_batch_workers` knob), `tests/test_momentum_live_runner_batch_parallel.py` (new, 9 tests).
- **Migrations:** none.

### The problem (confirmed from prod telemetry, not assumed)

`momentum_live_runner_batch` (every 30s) ticked each open live session **serially**. Each tick is network-bound (Coinbase quote/`get_product` + OHLCV entry-trigger fetch, ~seconds each). With ~5 concurrent live sessions the batch took the **serial sum** — `[scheduler_job] ... duration_ms` showed 35-42s worst case (3-15s typical), **overrunning the 30s interval**. Consequences (both handled, but degrading): the pullback-break/exit check effectively ran every ~40s instead of 30s; and the long batch overlapped the every-30s auto-arm pass on `trading_automation_sessions` row locks → benign `NOWAIT` `concurrent_tick` skips + noisy PG `could not obtain lock` lines.

Profiling note: `run_scheduler_job_guarded` already emits per-job `duration_ms`, so batch-level timing existed; the bottleneck is structural (the list query is a single indexed read, commits are local PG — only 5× serial network I/O explains 35-42s). The 5× variance for the *same* 5 serial sessions (3s→15s) is external provider/Coinbase latency, which parallelization caps at "slowest single session."

### The fix (scheduling/concurrency ONLY — entry/exit/risk untouched)

- `_dispatch_live_runner_ticks(session_ids, *, workers, tick_one)` runs each tick on a bounded `ThreadPoolExecutor`; `workers <= 1` or a single session ⇒ byte-identical serial loop (parity-tested).
- Each worker keeps the existing discipline: **its own `SessionLocal`** + **its own venue adapter** (the factory *is* the class), FIX-46 rollback-before-close preserved.
- The per-session `with_for_update(nowait=True)` row lock is **unchanged** — two batches (or the auto-arm pass) still can't double-process a session.
- Pool size **derives** from `chili_momentum_risk_max_concurrent_live_sessions` (no second magic number); new `chili_momentum_live_runner_batch_workers` (default `0` = derive) lets the operator throttle independently if Coinbase rate limits ever require it. Aligns with `feedback_adaptive_no_magic`.
- Added **profiling-grade per-batch telemetry** (one line, no per-session spam): `ticked N/M, wall, work_sum, slowest(sid), workers`.

### Thread-safety audit (live-money path — verified, not assumed)

- Coinbase REST client (process-global, shared): coinbase-advanced-py `RESTBase` auths **per-request** (`headers=` to `session.request`, never mutating the `requests.Session`) over urllib3's thread-safe connection pool → safe concurrent use.
- `idempotency_store` (dup client_order_id guard): lock-guarded mem cache + per-call `SessionLocal` + `ON CONFLICT DO NOTHING`. `rate_limiter`: `threading.Lock`. `fetch_ohlcv_df`: lock-guarded cache (already called concurrently by the auto-arm pass). `_reconcile_counters`: keyed by distinct sid → no same-key race.

## Verification

- **Unit:** 9 new tests (every session ticked once, serial/parallel parity, exception isolation, real-concurrency Barrier proof, own-session-per-tick, worker-cap derivation/override). 38 existing scheduler + live-runner tests still green.
- **Pre-flight:** imported the module + ran the dispatcher inside the built image before swapping the live container.
- **Live (post-deploy, `main-clean-0e51cd4` on `chili-clean-recovery-scheduler`, cron_only):**

  | batch | sessions | wall (new) | work_sum (old serial cost) | slowest tick |
  |-------|----------|-----------|----------------------------|--------------|
  | 1 | 5/5 | 3.8s | 10.2s | 3.80s |
  | 2 | 4/4 | 9.4s | 26.3s | 7.92s |
  | 3 | 5/5 | 8.1s | 28.5s | 7.39s |

  `wall ≈ slowest` every batch (concurrent), `wall ≪ work_sum`. Batch 3's serial cost (28.5s) was exactly the 30s-overrun case — now 8.1s. Zero tick failures/tracebacks. Deploy gate honored: swapped at **0 ENTERED** live positions; `CHILI_MOMENTUM_ENTRY_TRIGGER_MODE=pullback_break` + all go-live env preserved. Rollback handle: container `chili-recovery-scheduler-prem-d8656fe` (stopped, on `main-clean-d8656fe`).

## Surprises / deviations

- Prod batch durations were 3-15s at observation time (worst case 35-42s is intermittent — correlated with ENTERED sessions polling exits and/or provider latency spikes), not a steady 35-42s. The fix targets exactly that tail: it bounds the batch at the slowest single tick regardless of session count.
- A parallel agent merged `#521` (one-shot reconcile-exit backfill) onto main during this task; disjoint files, merged without conflict. Deployed image `main-clean-0e51cd4` == PR #522 content; `#521` is a one-shot data script not needed by the scheduler runtime. Tag hygiene only: the operator may optionally rebuild `main-clean-4fa217a`, but it is content-equivalent for this container.

## Deferred

- **`auto_arm` row-lock contention (Symptom 1):** the shorter batch window already shrinks the overlap with the every-30s auto-arm pass; the NOWAIT skips won't vanish entirely (auto-arm still UPDATEs session rows under its own transaction). Not pursued further — out of scope (would touch auto-arm semantics) and now low-impact.
- **brain_work backtest dispatcher idle-in-transaction** (`brain_work/dispatcher.py`): confirmed running in this same scheduler container (`FractionalBacktest.run` in logs) and contending for CPU; explicitly out of scope per the brief. Handled today by `_recover_dispatch_session` invalidate; hygiene debt remains.

## Open questions for Cowork

- Should the live runner move to its **own container** (away from the backtest dispatcher's CPU contention), or is the now-comfortable headroom (8s in a 30s window) sufficient? The parallel batch absorbs the contention fine today.
- With headroom restored, is there appetite to **tighten the cadence** (e.g. 15s) for faster Ross scalping, or raise `max_concurrent_live_sessions`? The pool scales with the cap automatically.
