# CC_REPORT: f-momentum-asymmetric-exit

Ross Cameron's asymmetric exit structure for the `momentum_neural` lane (M4 exit
component). Direct operator instruction this session — supersedes the stale
`NEXT_TASK.md` (which was queued to `f-position-identity-phase-5i-post-rename-soak`,
a different initiative). Flagged in Open Questions.

## What shipped

Branch `chili/momentum-asymmetric-exit` (isolated worktree off `origin/main`
`4276cb9`). **Commit `<HASH>`.** 7 files changed + 1 new test file. No migrations.

The lane did a **2:1-then-flat** exit — live dumped 100% at the first target;
paper took an ad-hoc `orig_qty/3.0` partial at the 1R-halfway (a magic number,
paper-only → a parity violation) then dumped the rest at target. Both "TRAILING"
states used a **static** `entry × trail_floor_return` floor that never ratcheted.
Net: the runner/tail was forgone — exactly the "~flat exit, no runner" the
2026-06-07 audit found.

Replaced with Ross's verified structure in BOTH runners:

1. **First-target partial** (`STATE_*_SCALING_OUT`): sell
   `chili_momentum_scale_out_fraction` of the **original** size (default 0.5 =
   "sell 1/2"). The 2:1 reward:risk for the first target is unchanged.
2. **Breakeven on the balance**: the runner's stop moves to the entry price
   (derived; ratchet-only).
3. **Hold + trail the runner** (`STATE_*_TRAILING`): chandelier off the
   high-water mark at the same ATR distance the initial stop used
   (`atr_pct × stop_atr_mult`, derived from the frozen entry ATR). The first-target
   partial now fires from ENTERED **or** TRAILING (price can pass trail-activate
   before the target), guarded by `partial_taken` so it fires once.

Tiny positions that can't leave a venue-sellable runner fall back to a flat exit
at target (never strand un-sellable dust).

**One documented knob:** `chili_momentum_scale_out_fraction` (default 0.5,
`gt=0, lt=1`). Breakeven = entry (derived). Trail = chandelier off frozen entry
ATR (derived). No other new numbers — honors the no-magic-numbers rule.

**Parity contract:** the exit math lives in `paper_execution.py`
(`scale_out_fraction`, `breakeven_stop_after_partial`, `scale_out_quantity`,
`runner_trail_stop`); both runners import the identical functions, so backtest and
live take the same structural decision by construction.

Files: `app/config.py` (knob), `paper_execution.py` (4 shared helpers),
`live_runner.py` + `paper_runner.py` (FSM wiring + `_commit_*` hardening),
`live_fsm.py` + `paper_fsm.py` (`TRAILING → SCALING_OUT` transition),
`docs/DESIGN/MOMENTUM_LANE.md` (§8), `tests/test_momentum_asymmetric_exit.py`.

## Verification

- **New suite `test_momentum_asymmetric_exit.py`: 10/10 pass.** Covers the pure
  helpers (fraction clamp, breakeven ratchet, split + dust guard, chandelier
  ratchet/floor/never-loosen), the parity contract (both runners share the
  identical helper objects), and full live + paper integration: a winner hits the
  first target → sells 0.5 → balance stop = entry (breakeven) → runner held →
  chandelier ratchets the stop up → pullback trips the trailed runner stop. Both
  runners net **+6.25** vs the old flat-2:1 **+4.0** (runner captured the tail) —
  the thesis, asserted in code.
- **Existing momentum + live-exit regression (6 files, 73 tests): green** after a
  follow-up fix (below). `test_momentum_live_runner`, `test_momentum_paper_runner`,
  `test_live_runner_exit_gating`, `test_pending_exit_liveness`,
  `test_live_exit_broker_zero_reconcile`, `test_momentum_neural_settings_closeout`.
- Live runner remains OFF behind `chili_momentum_live_runner_enabled` (operator
  go-live gate, unchanged). The structure is live + on (no dark flag) — it
  activates when the lane enters; manifests on the first post-keystone entry.

## Surprises / deviations

- **SQLAlchemy mutable-JSON silent-drop (found + fixed).** The scale-out commits
  twice in one tick around `_apply_confirmed_live_partial_exit`'s event-emit
  flush. The reassigned `risk_snapshot_json` compared EQUAL to the flush-pinned
  baseline (shared nested `position` ref, mutated in place), so SQLAlchemy skipped
  the second UPDATE and the breakeven move was silently lost (reverted to the
  pre-breakeven stop on commit+expire). Fix: `_commit_le` / `_commit_pe` now call
  `flag_modified(sess, "risk_snapshot_json")` (guarded with try/except so the
  SimpleNamespace unit-test doubles, which have no ORM state, still work). This is
  a latent-bug fix that hardens *every* multi-commit-per-tick path, not just the
  scale-out.
- **Removed paper's halfway-1/3 partial.** It was paper-only (live had no
  equivalent → a standing parity violation) and a hardcoded `/3.0`. Replaced by
  the unified Ross scale-out at the 2:1 target. This changes paper backtest
  behavior — intentional, in service of Ross-faithfulness + backtest/live parity.
- `trail_floor_return_bps` is now vestigial for the trail in both runners (the
  static floor it fed is gone). Left in `strategy_params` (still tuned by the
  refiner) to avoid a wider signature change; harmless. Flagged for a later prune.

## Deferred

- **Structural-low trail.** The runner trail is an ATR-chandelier (derived,
  parity-symmetric, no extra fetch in live). Ross also trails to the literal next
  pullback low; `entry_gates._compute_confirmed_swing_low_last` exists and paper
  already fetches OHLCV each held tick. A structural tighten could layer on top
  (chandelier as the floor) — deferred to keep this change parity-clean and avoid
  a per-tick OHLCV fetch in the live held path.
- **Adaptive scale-out fraction.** Kept as one static knob (0.5) per the brief.
  The lane refiner could learn it per family (like the other params) — natural
  follow-up.
- Pruning the vestigial `trail_floor_return_bps` from `strategy_params`.

## Open questions for Cowork

1. `NEXT_TASK.md` still points at `f-position-identity-phase-5i-post-rename-soak`
   (STATUS: PENDING) — untouched. This session executed the operator's direct
   asymmetric-exit instruction instead. Re-queue 5i, or fold it into the next
   brief?
2. Default scale-out fraction 0.5 (largest runner / most tail) vs 0.6–0.75 (more
   into strength). Ross uses both depending on setup. Leaving at 0.5; flag if you
   want it higher or learner-driven from the start.
3. Want the structural-low trail tighten layered next, or is the ATR-chandelier
   sufficient for the first live observation window?
