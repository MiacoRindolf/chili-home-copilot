# CC_REPORT: momentum-reconcile-exit-outcome-class

> Direct operator brief (flagged follow-up to #517, the durable-entry fix).
> **NEXT_TASK.md is untouched** — it is still `f-position-identity-phase-5i-post-rename-soak`,
> an unrelated initiative. This task did not change phase-5i state. Flagged per
> the PROTOCOL one-task-one-commit rule.

## What shipped

- **PR #518** — `fix(momentum-lane): classify reconcile round-trips by true exit
  class, not cancelled_in_trade`. Branch `chili/reconcile-exit-outcome-class`,
  squash-merge **d8656fe**. CI full-suite green (no flake this run). A sibling
  merged **#519** (auto-arm SKIP observability, 17afd74) concurrently; d8656fe is
  the post-merge main tip carrying both — re-fetched right before building per
  [[feedback_sync_before_change]].
- Files touched: 4 (1 service + 3 test/script).
  - `app/services/trading/momentum_neural/outcome_extract.py` — the fix.
  - `tests/test_outcome_reconcile_exit_class.py` — new (38 tests).
  - `tests/test_outcome_entry_occurred_durable.py` — updated the #517-era case.
  - `scripts/d-verify-reconcile-exit-class.py` — new read-only verification.
- Migrations added: 0 (pure labeling logic; no schema change).

### The bug

A live momentum session that completes a REAL round-trip — entry + a full exit
recorded via the broker-zero-reconcile path — can terminate in FSM state
`live_cancelled` (the recycled post-exit watcher is reaped, or a duplicate
claimant is cleaned up) rather than `live_finished`. `derive_outcome_class` then
labeled it `cancelled_in_trade`, a non-strategy outcome
(`contributes_to_evolution=False`) the selection learner only readmits via the
`stop_too_tight` shake-out back-door (`evolution._contributes_or_shakeout_filter`).
So a clean reconcile WIN or a **non-shakeout** reconcile LOSS was invisible to
the strategy learner — real wins/losses silently dropped from learning.

### The fix (option a — pure labeling, lowest blast radius)

When a `live_cancelled`/`cancelled` terminal carries entry + a recorded FULL
exit reason, classify it by its true exit class — stripping the
`_broker_zero_reconcile` / `_retry_cap_broker_zero_reconcile` provenance suffix
first — identical to how the finished branch labels the same exit. Extracted the
finished-branch body into a shared `_classify_real_exit` helper used by both
terminal branches (so a real round-trip is labeled identically whether it ended
`live_finished` or got cancelled after the exit reconciled). The reroute only
ever upgrades to a genuine exit class (`stop_loss`/`bailout`/`timed_exit`/
`success`/`small_win`/`governance_exit`); anything ambiguous falls through to
`cancelled_in_trade`. A position-neutral operator/dup cancel of a still-open
position (no full exit reason — a partial sets `last_partial_exit_reason`, not
`exit_reason`) correctly stays `cancelled_in_trade`.

## Verification

- **Local tests (chili_test):** 38 new + the #517 durable-entry test updated;
  `test_momentum_feedback_phase9.py` (12) + `test_canonical_outcome_layer.py` (8)
  + `test_crypto_broker_zero_reconcile.py` + `test_live_exit_broker_zero_reconcile.py`
  + `test_post_exit_excursion.py` (33) all green. ~97 tests across the affected
  surface.
- **Live read-only re-derivation** (`scripts/d-verify-reconcile-exit-class.py`
  vs the live `chili` DB, 40 cancelled rows):
  - **17 real round-trips relabeled**: 15 → `stop_loss`, 2 → `bailout`.
  - `contributes_to_evolution` flips True for the round-trips with an entry
    decision packet + economic result.
  - Position-neutral holding cancels (sess 7/24/54: entry, no exit reason) stay
    `cancelled_in_trade`. Pre-entry cancels stay `cancelled_pre_entry`,
    including sess 20 (`max_hold_broker_zero_reconcile` but entry unprovable +
    no economic result → conservatively stays pre-entry per the
    `entry_occurred AND` gate).
- **Aggregate correctness:** `_aggregate_rows` keys its return / setup-adjusted
  channels on `return_bps` + the post-exit label, NOT on `outcome_class`. A
  `stop_too_tight` row that was already counted via the back-door is now counted
  via `contributes_to_evolution=True` — still counted ONCE; `mean_return_bps`
  and the setup-adjusted mean are invariant (unit-tested:
  `test_aggregate_invariant_to_relabel_for_shakeout_row`). The net change for
  those rows is only the contributes flag; the non-shakeout reconcile losses are
  the genuine new learner inclusions.
- **Consumers checked (no double-count):** `brain_desk_summary` (mix_top counts
  each row once; best/weakest_variant use `return_bps`, unaffected by the label),
  `feedback_query` (read-model passthrough; `evolution_credit_diagnostics`
  credited-count rises as intended), `evolution` aggregate (OR filter dedupes per
  row). All correct.
- **Deploy gate:** 0 position-holding live momentum sessions at swap time (5
  active sessions, all pre-entry `watching_live`/`queued_live`).

## Deploy

- Built `chili-app:main-clean-d8656fe` from an isolated clean worktree checked
  out at the post-merge `origin/main` tip (the shared main tree is hammered by a
  parallel codex agent — [[feedback_sync_before_change]]).
- Atomic-swapped `chili-clean-recovery-scheduler` (the cron_only container that
  runs the momentum lane, the post-exit labeler, and
  `emit_feedback_after_terminal_transition` → `ingest_session_outcome`) onto the
  new image, with **0 position-holding live sessions** at swap time. A sibling
  agent had redeployed the scheduler to `main-clean-17afd74` (#519) during the CI
  wait; d8656fe = 17afd74 + #518, so the swap was a clean forward move. Previous
  image kept as rollback `chili-clean-recovery-scheduler-prem31`
  (`main-clean-17afd74`).
- Boot verified: `Scheduler started role=cron_only`, 60 jobs registered, **0
  tracebacks**, `momentum_live_runner_batch phase=ok`. Fix confirmed live via an
  in-container import smoke test (`trail_stop_broker_zero_reconcile` →
  `stop_loss`; position-neutral cancel → `cancelled_in_trade`).
- Keystone `CHILI_MOMENTUM_ENTRY_TRIGGER_MODE=pullback_break` preserved (env-file
  `D:/CHILI-Docker/_sched_261364e.env` unchanged; running-container env diffed
  against the file — no drift). Live runner + auto-arm flags unchanged.

## Surprises / deviations

- The mechanism that lands a completed round-trip in `live_cancelled` is the
  **recycled watcher being reaped** (or dup-claimant cleanup), NOT the reconcile
  transition itself — the live_runner reconcile path goes to `live_exited` →
  `cooldown`, which can recycle to `watching_live`; if the recycled watcher is
  then cancelled it carries the prior trade's durable `last_exit_reason` +
  `realized_pnl`. This validated option (a): the cancellation is legitimate, only
  the LABEL was wrong.
- Some persisted rows (sess 8/11/51/57) were `cancelled_pre_entry` with a real
  exit_reason + P&L because they predate the #517 deploy. The fix composes with
  #517: at the next extraction `entry_occurred` is durable-True (realized P&L
  present) → they route to `stop_loss`. They are NOT auto-corrected in place —
  see Deferred.

## Deferred

- **Historical backfill of the 17 persisted rows is NOT applied.** Re-deriving
  and re-ingesting them would feed pre-keystone-fix losses into the
  viability/kill/refine learner. `maybe_kill_underperforming_variant` deactivates
  a variant at `win_rate < 0.35 AND mean_return_bps < -30 AND n >= 5`; the lane is
  historically ~0-win, so a bulk backfill could KILL the momentum variants on
  losses that predate the pullback_break + structural-stop fixes. The go-forward
  fix is safe (new sessions label + contribute correctly). The dry-run script
  shows exactly what a backfill would change. **Open question for Cowork below.**
- `scripts/d-verify-reconcile-exit-class.py` is dry-run only (no `--apply`).

## Open questions for Cowork

1. **Backfill the 17 historical rows?** It realizes the learning value on the
   EIGEN + earlier sessions, but re-ingests historical losses that could trip the
   variant-kill gate on pre-keystone-fix performance. Options: (a) leave history,
   go-forward only (current state, safest); (b) relabel-only (fix
   `outcome_class` + contributes for dashboards/tallies) WITHOUT running the
   kill/refine side effects; (c) full backfill + re-ingest. Recommend (b) if the
   operator wants the dashboards correct without risking a kill on stale losses.
2. Sess 20-class rows (`*_broker_zero_reconcile` reason, entry unprovable, no
   economic result) stay `cancelled_pre_entry`. Acceptable (no economic result →
   never contributes), but the label is slightly imperfect. Leave as-is?
