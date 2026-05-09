# f-pattern-demote-sweep-wiring-fix

STATUS: QUEUED
SLUG: pattern-demote-sweep-wiring-fix
PROPOSED: 2026-05-08
SEVERITY: medium-high (sweep code is correct but never fires; hand-kick was needed to demote pattern 585; future thin-evidence patterns won't be caught autonomously)

## TL;DR

`f-pattern-demote-on-thin-evidence` (commit `dfb39f0`) shipped today
with the correct sweep code — `run_thin_evidence_demote(db)` works
when called directly, demoted pattern 585 in <1s on hand-kick at
2026-05-09 00:04:25 UTC. **But the wiring is dead**: CC hooked into
`_handle_execution_feedback_digest` (event-driven, work-ledger-
triggered), which brain-worker logs show is firing at
`processed=0 claimed=0` per cycle — it requires an upstream producer
to enqueue an `execution_feedback_digest` work-ledger row, and
nothing is producing them. Pattern 585 stayed `promoted` for 75+
minutes after the restart that should have demoted it within 1-2
brain cycles.

Re-wire the sweep to a hook that genuinely fires per-cycle.

## Why now

Verification post-restart of brain-worker + scheduler-worker
(2026-05-08 23:57 — 00:02 UTC, ~5 minutes of dispatcher rounds):

```
[brain] work ledger dispatch round processed=0 claimed=0
  per_type={'execution_feedback_digest': 0, 'market_snapshots_batch': 0,
            'backtest_requested': 0, 'backtest_completed': 0,
            'pattern_eligible_promotion': 0, 'live_trade_closed': 0,
            'paper_trade_closed': 0, 'broker_fill_closed': 0,
            'breakout_alert_resolved': 0} errors=[]
```

ALL 9 dispatcher event types are at 0/cycle. The work ledger is
empty. The event-driven hook is unreachable in the current operating
state — there are no live trades closing right now (PDT count is 0,
the autotrader hasn't placed an entry today), so nothing produces
the upstream events.

Hand-kicked the sweep:
```
result: {'ok': True, 'demoted': 1, 'demoted_ids': [585]}
```

Pattern 585 → `challenged`. Alerts dropped from 158/24h to 0 in
5 min (the alert pipeline correctly filters `lifecycle_stage !=
'promoted'`).

## Goal

Wire `run_thin_evidence_demote` to a hook that fires every brain
cycle regardless of whether upstream events have produced work-
ledger rows. Two natural choices:

**Option A**: Add to the brain-worker's per-cycle sweep (the same
loop that emits the `[brain] work ledger dispatch round` log).
The sweep runs every ~75-90s today (visible in the timestamps).
Call `run_thin_evidence_demote(db)` once per round, after the
existing dispatcher fanout.

**Option B**: Add as a scheduler cron job (every 5-10 min) under
`scripts/scheduler-worker.py`. Cleaner separation: the brain-
worker does event-driven work; the scheduler does scheduled
sweeps.

**Recommended: Option A.** The sweep is cheap (one SQL SELECT +
one UPDATE on a tiny rowset), idempotent on re-run, and
conceptually belongs with the brain's existing realized-sync work.
Operator's preference for "the brain manages the brain" maps to
keeping it in brain-worker.

## The change

In `app/services/brain_work/dispatcher.py` (or wherever
`run_brain_work_dispatch_round` lives):

* Locate the per-round entry point. It already calls the existing
  dispatcher fanout.
* After the fanout, call `run_thin_evidence_demote(db)` inside a
  try/except so a sweep failure doesn't poison the round.
* Log demoted IDs at INFO level (today's `dfb39f0` work folds the
  count into the dispatcher outcome payload — that's still useful
  but operator wants direct grep visibility too).
* **Remove** the per-event hook in
  `_handle_execution_feedback_digest` to avoid double-execution
  (or keep it gated behind a `if not _PER_CYCLE_SWEEP_ENABLED:`
  flag so a single source of truth is maintained).

## Acceptance criteria

1. Per-cycle hook lives in `dispatcher.py` and runs on every
   `run_brain_work_dispatch_round` invocation, not on
   `execution_feedback_digest` event.
2. `_handle_execution_feedback_digest` is either:
   - Removed of the thin-evidence sweep call (single source of truth
     in the per-cycle hook), OR
   - Wrapped with `if not _PER_CYCLE_SWEEP_ENABLED:` so only one
     of the two fires.
3. New helper-level test in
   `tests/test_pattern_demote_sweep_wiring.py`:
   - **per-cycle hook calls sweep**: mock the dispatcher round,
     assert `run_thin_evidence_demote` is called.
   - **sweep failure doesn't poison the round**: simulate a sweep
     exception, assert the round still completes and other
     dispatch work runs.
4. Live verification post-deploy: pattern 585 stays
   `challenged` (it's already there from the hand-kick); seed a
   fake thin-evidence pattern in chili_test, run one dispatcher
   round, assert it gets demoted within one round.
5. Existing 15 thin-evidence tests still pass (the predicate +
   sweep code is unchanged).
6. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pattern-demote-sweep-wiring-fix.md`.

## Brain integration (reuse, don't rewrite)

- `app/services/brain_work/dispatcher.py` — the `run_brain_work_
  dispatch_round` function. Add the sweep call after the existing
  fanout.
- `app/services/trading/learning.py:run_thin_evidence_demote` —
  unchanged. Already idempotent and cheap.
- The four threshold constants
  (`THIN_EVIDENCE_MIN_TRADES` etc.) — unchanged.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Don't touch the sweep predicate or the SQL UPDATE.** They're
  correct; only the wiring is broken.
- **Don't touch the four threshold constants.** They're already
  module-level lifts.
- **No magic numbers in this brief either.** The dispatcher round
  cadence (~75-90s) is whatever it is; don't hardcode a different
  value here.
- **Edit-tool truncation discipline (HARD).**
  `app/services/brain_work/dispatcher.py` is small, but verify
  with `wc -l + ast.parse` post-edit.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Re-promotion path.
- Other lifecycle gaps (consecutive-loss-demote, regime-mismatch-
  demote).
- The other queued briefs (`f-pdt-crypto-bypass-cleanup`,
  `f-autotrader-pdt-aware-exit-deferral`).
- Switching to Option B (scheduler cron). If Option A doesn't fit
  cleanly, fall back to B and surface in the CC report.

## Sequencing

1. Truncation scan on `dispatcher.py`.
2. Locate `run_brain_work_dispatch_round` and confirm round-cadence
   logging.
3. Add the post-fanout sweep call inside try/except.
4. Either remove the `_handle_execution_feedback_digest` sweep call
   OR gate it on the `_PER_CYCLE_SWEEP_ENABLED` flag.
5. Tests.
6. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch the brain-worker logs:
   ```
   docker logs chili-home-copilot-brain-worker-1 -f --tail 0 | grep -i thin_evidence
   ```
   Expected: a `[learning] thin_evidence sweep: demoted=N ids=[...]`
   line every dispatcher round (~75-90s), with `demoted=0` once 585
   is already challenged and no new candidates exist.
4. (Optional smoke) Insert a fake thin-evidence pattern row in
   `chili_test`, run one dispatcher round, verify it gets demoted.

## Rollback plan

`git revert` the commit. The per-cycle hook is purely additive;
revert restores the prior event-driven hook (still present and
correct, just dormant). Pattern 585 stays `challenged` regardless
because lifecycle_stage doesn't auto-revert.

## Open questions

1. **Why is the work ledger empty?** This is a separate question
   surfaced by the verification: ALL 9 event types are at 0/cycle.
   Either nothing in the system is producing work-ledger rows, or
   the producers exist but are gated on conditions that aren't
   met today (no live trades, no fresh backtest results, etc.).
   Surface in CC report; if the work ledger is structurally
   broken, that's a separate brief.
2. **Should the wiring fix be retroactive?** Pattern 585 already
   demoted via hand-kick. Once the per-cycle sweep is live, it
   won't re-touch 585 (lifecycle_stage filter). The 1011/1016
   patterns stay promoted (they don't match the criteria). So
   the wiring fix's first effect will be on the NEXT thin-evidence
   pattern that gets promoted via `provisional_small_paths` — at
   today's brain cadence, that may be days or weeks away.
3. **Brain-worker dispatcher cadence stability.** Per logs:
   23:57:12 → 23:58:22 (70s) → 23:59:38 (76s) → 00:00:59 (81s) →
   00:02:21 (82s). Cadence is ~75-90s. If this is unstable
   (e.g., scales with work-ledger backlog size), the sweep
   becomes erratic. Surface in CC report; if needed, add a
   minimum-cadence guard (don't run sweep more than once every
   60s) or move to Option B (scheduler cron).
