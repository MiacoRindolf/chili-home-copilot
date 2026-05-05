# CC_REPORT: f-kill-legacy-learning-cycle

## What shipped

One commit. No migrations. Pure config + ~30 lines of `scripts/brain_worker.py`
+ a Phase 2 backlog doc.

**Files touched (3):**

- `scripts/brain_worker.py`:
  - **Step 3** — `_RECONCILE_PASS_MAX_INTERVAL_S` default raised from `4 * 3600` (4h) to `365 * 24 * 3600` (1y). The legacy cycle is gated off; the safety floor is moot. Operator can re-engage a real floor by setting `CHILI_BRAIN_RECONCILE_MAX_INTERVAL_S=14400`.
  - **Step 2** — `_should_skip_reconcile_pass` cold-start branch flipped from `return False, "cold_start_first_cycle"` (force-trigger) to `return True, "cold_start_no_auto_trigger"` (skip + initialise watermark). Belt-and-suspenders so even a re-enabled cycle doesn't fire on every restart.
  - **Step 1** — `_run_lean_cycle_loop` gates the existing `_should_skip_reconcile_pass` call behind `CHILI_BRAIN_LEGACY_CYCLE_ENABLED` env var (default `0`). When disabled, every iteration emits `[brain] legacy run_learning_cycle DISABLED ...` and skips with `skip_reason="legacy_cycle_disabled"`. When `=1`, falls through to the existing FIX-31 gate behaviour.
- `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` — **new doc**. Inventory of every cycle step + handler-coverage status. Headline correction: contrary to the brief, **all five Phase 2 handlers (mine, cpcv_gate, promote, demote, regime_ledger) are already wired and shipping** per `dispatcher.py:272-321`. The actual coverage gap is the ~18 OTHER cycle steps that have no handler equivalent. Top of the priority list is `f-handler-pattern-stats` because today's `f-evidence-canonical-writer` depends on it firing.
- `docs/STRATEGY/NEXT_TASK.md` — marked `STATUS: DONE`.

**Migrations added: 0.**

## Verification

### Gate-decision smoke (executable)

Ran a synthetic exercise of the gate logic with default env vars:

```
safety_floor_default_s: 31536000   (= 1 year)
cold_start_skip: True cold_start_no_auto_trigger
watermark_initialized: True
legacy_cycle_default_disabled: True
legacy_cycle_explicit_enable_works: True

ALL SMOKE CHECKS PASS
```

Confirms:
- The 1-year default landed.
- Cold-start no longer auto-triggers; the watermark gets initialised so subsequent gate calls see a "real" elapsed reading.
- The kill-switch env var is correctly default-off.
- The explicit re-enable path (`CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1`) flips through.

### Regression

```
pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -p no:asyncio
> 248 passed in 1.66s
```

No tests touch `_run_lean_cycle_loop` directly (it's the worker entry point), so test-suite regression is the upper bound; no behavioural test exists for this gate.

### Smoke deferred to deploy (environment-side)

Per brief Step 5, real verification requires:

```bash
docker compose restart brain-worker
# wait 30 minutes
docker compose logs brain-worker --since 30m \
  | Select-String "run_learning_cycle|reconcile_pass_completed|legacy_cycle_disabled"
```

Expected post-deploy:
- `legacy_cycle_disabled` log line on every loop iteration.
- Zero `learning_cycle_end` events.
- Zero `psycopg2.OperationalError: server closed the connection` events.
- `pg_stat_activity` filtered to `application_name='chili-brain-worker'` AND `state='idle in transaction'` AND `state_change < NOW() - INTERVAL '5 minutes'` returns 0.
- `docker stats` brain-worker memory < 1 GiB, CPU < 50% of one core.

These are environment-side; documented for the next operator review.

## Surprises / deviations

### 1. **Brief's "only handler #1 shipped" framing is out of date**

The brief stated: *"only handler #1 (mine, FIX 36) shipped on 2026-04-29; handlers #2-5 (cpcv_gate, promote, demote, regime_ledger) stalled and the legacy cycle has been the fallback ever since."*

Inspection reveals **all five handlers are already wired**:

```
app/services/trading/brain_work/handlers/
├── mine.py           (FIX 36, 2026-04-29) — market_snapshots_batch
├── cpcv_gate.py      (FIX 37, 2026-04-29) — backtest_completed
├── promote.py        (FIX 38, 2026-04-29) — pattern_eligible_promotion
├── demote.py         (FIX 39, 2026-04-29) — live/paper/broker trade_closed
└── regime_ledger.py  (FIX 39, 2026-04-29) — same trade-close events
```

`dispatcher.py:272-321` dispatches all five. They aren't stubs — each has a real `handle_*` function with documented contract.

This narrows the impact assessment. **The legacy cycle being disabled does NOT mean the brain stops mining/promoting/demoting** — those flow through the event handlers. What it DOES mean is that the ~18 cycle-only steps catalogued in `PHASE2_HANDLER_BACKLOG.md` stop running. Most are research/journal/UI steps; the load-bearing one is `update_pattern_stats_from_closed_trades` (just-shipped `f-evidence-canonical-writer`), which is documented as the top of the Phase 2 backlog.

The Cowork plan in `reference_phase2_event_handlers.md` (saved memory) likely needs an update — but I left it untouched per "do not modify saved memory I didn't write."

### 2. **The brief's "Phase 2 sequencing recommendation" needs revision**

The brief asked which handler is most operator-impactful to ship next. Brief's options were:
- (a) `f-handler-pattern-stats`
- (b) `f-handler-cpcv-gate + f-handler-demote`

But (b) is already shipped. So the actual recommendation is:

1. **`f-handler-pattern-stats`** — completes the f-evidence-canonical-writer chain.
2. **`f-handler-breakout-outcomes`** — covers patterns with no closed trades.
3. **`f-handler-validate-evolve`** — keeps weight evolution moving.
4. **`f-handler-live-drift` + `f-handler-execution-robustness`** — drift / execution-quality monitoring (probably bundleable).

Full ordered list in `PHASE2_HANDLER_BACKLOG.md`.

### 3. **Cycle-only steps that aren't event-natural**

Several cycle steps don't have an obvious event trigger and would need a timer-based handler (cron-like):
- `decay_stale_insights` (timer)
- `seek_pattern_data` (timer)
- `dead_ticker_cleanup` (timer)
- `daily_market_journal` (cron)
- `pattern_ml.train` (timer)
- `live_pattern_depromotion` (sweep mode — for patterns whose trades stopped firing entirely)

These are surfaced in the backlog so the Phase 2 brief author can choose between (a) per-event handlers where natural, (b) timer/cron handlers where not, or (c) just dropping them as no-longer-needed.

### 4. **No code deletion in this brief**

Per brief constraint: `run_learning_cycle` source stays in `learning.py:9329` and `scripts/brain_worker.py:803`. Just the invocation is gated off. Emergency rollback via `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1` re-engages the cycle exactly as before, no code change required.

`update_pattern_stats_from_closed_trades` also stays callable; will be wired into a handler in `f-handler-pattern-stats`.

## Audit summary

- **No new magic numbers.** `365 * 24 * 3600` is documented inline as "1 year, effectively never; operator overrides via env var."
- **No live-broker behaviour change.** The kill switch only affects brain-worker's learning loop.
- **No safety-belt changes.** Fast-path live-placement belts untouched (PROTOCOL Hard Rule 1).
- **No threshold tuning.** This brief disables a path; doesn't tune any threshold.
- **Emergency rollback is one env var.** Set `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=1` in compose.yml, restart brain-worker, cycle re-engages.

## Deferred (explicitly not in this task)

- **Shipping any handler.** Phase 2 briefs.
- **Deleting `run_learning_cycle` source.** Final cleanup brief once all of `PHASE2_HANDLER_BACKLOG.md` is shipped or retired.
- **DB-stability config (TCP keepalives, pool_pre_ping).** The cycle disable removes the long-idle-tx pattern; if other long-running queries surface drops, separate brief.
- **Pattern-evidence backfill via the now-disabled cycle path.** The data we have is what we have until `f-handler-pattern-stats` ships.

## Open questions for Cowork

1. **Brief's stale "handlers #2-5 stalled" framing.** All five Phase 2 handlers are already shipped per `dispatcher.py`. Saved memory `reference_phase2_event_handlers.md` likely needs updating to match. Surfaced as Surprise §1.

2. **`update_pattern_stats_from_closed_trades` is now the highest-impact uncovered step.** Today's f-evidence-canonical-writer corrections are dead-coded until `f-handler-pattern-stats` ships. Recommend that as the immediate Phase 2 next-up.

3. **Timer-based handlers**: several cycle steps don't have a natural per-event trigger (`decay_stale_insights`, `seek_pattern_data`, `daily_market_journal`, `pattern_ml.train`, etc.). Should those become timer-based handlers in `brain_work/`, OR move to `scheduler-worker` as APScheduler jobs (which already exist for some periodic work)? Surface as a strategy decision before writing the briefs.

4. **Sweep-mode depromotion gap**: `handlers/demote.py` is per-trade-close. Patterns whose trades have STOPPED firing won't get re-checked — they stay at `lifecycle_stage='promoted'` indefinitely. The cycle's `run_live_pattern_depromotion` was the sweep that caught these. Need a timer-based `f-handler-stale-promoted` or accept the gap (the operator can demote manually when noticed).

5. **Other long-running queries**: post-deploy verification should also check whether any *other* worker (scheduler-worker, momentum-runner) holds 60+ minute transactions. The cycle disable only fixes the cycle; other long queries (e.g., `momentum_symbol_viability` flagged in earlier diagnostics) may need their own treatment. Surface in the next review if the post-deploy data shows new culprits.

## Stale uncommitted work (carry-forward)

Pre-existing at session start, untouched: `app/models/trading.py` `_trade_phantom_close_guard` event listener (still in working tree, unstaged), `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags, `data/ticker_cache/crypto_top.json` byte-shift, untracked `.commit_msg_*.txt` / `docs/AUDITS/*` / `docs/STRATEGY/COWORK_REVIEWS/*` backlog. Same disposition as prior CC reports: left exactly as found.
