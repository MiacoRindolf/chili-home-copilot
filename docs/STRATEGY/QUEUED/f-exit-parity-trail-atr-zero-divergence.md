# f-exit-parity-trail-atr-zero-divergence

## Goal

Close the only remaining `legacy_only_close` divergence the cutover-gate
verdict surfaces in 24h: 39 backtest rows where legacy fires
`exit_trail` and canonical holds, all attributable to a single root
cause — **how the two engines treat bars with ATR = 0**. Once closed,
the backtest source moves from `FAIL_ASYMMETRIC_AGGRESSIVE` to `PASS`,
which (combined with live-source soak) unblocks the
`shadow → authoritative` cutover.

## Why now

The 2026-05-09 cutover-gate monitor reported:

- **live**: `INSUFFICIENT_DATA` (530 `both_hold`, 0 `both_close`)
- **backtest**: `FAIL_ASYMMETRIC_AGGRESSIVE`
  - 283,987 `both_close` (bias=0.0 bps, te=0.0 bps — perfect agreement
    on exit price when both close)
  - 39 `legacy_only_close`, 0 `canonical_only_close`
  - canonical_aggressive_share = 0/39 = 0.000 → trips the < 0.4 gate
  - **all 39** asymmetric closes have `priority_winner = 'exit_trail'`

The 39 rows are 0.014% of total — cosmetically tiny — but the gate is
binary and won't relax. We can't ship the cutover with a structural
disagreement on file, even a small one.

## Root cause (verified by reading both code paths)

**Legacy** (`app/services/backtest_service.py:1322-1346`):
```python
atr_val = 0.0
if i < len(self._atr_array) and self._atr_array[i] is not None:
    atr_val = self._atr_array[i]
trailing_stop = self._highest_since_entry - self._exit_atr_mult * atr_val
# ...
if price < trailing_stop:
    legacy_action_str = "exit_trail"
```

When `atr_val == 0`, this collapses to
`trailing_stop = highest_since_entry`, which fires `exit_trail` on
**any pullback from the running peak**. That isn't a trailing stop on
ATR=0 bars — it's a fixed peak-stop. Almost certainly unintended.

**Canonical** (`app/services/trading/exit_evaluator.py:203-205`):
```python
atr = _safe_float(bar.atr, None)
if atr is None or atr <= 0:
    return state.trailing_stop
```

When ATR is None/0, canonical returns the previous bar's trail
unchanged (or `None` if no trail is set yet). This is a more defensive
behavior — never fires `exit_trail` on a degenerate-volatility bar.

The shadow harness at `backtest_service.py:1427` makes the divergence
explicit: `atr=atr_val if atr_val > 0 else None` is the bridge that
flips legacy's "0" to canonical's "None".

## The decision (where this brief needs operator input)

There are three plausible directions. **Recommendation: Option A**, but
operator should confirm before CC executes.

### Option A — fix legacy (recommended)

Treat `atr_val == 0` in legacy as "skip the trail check this bar"
(mirror canonical). Closes the gap by removing 39 spurious legacy
closes; canonical and legacy agree on hold.

Pro:
- The legacy behavior (`trail = peak`) is almost certainly a bug.
  Removing it removes spurious closes that were eating into backtest
  performance with no economic justification.
- Cutover-gate passes for the right reason.

Con:
- Backtest results change (the 39 hold-instead-of-close decisions
  ripple into final P/L for those test runs). Backtest results feed
  promotion gates (CPCV, EV). Need to assess promotion-gate stability.
- "Bug-for-bug parity" purists will note we're changing legacy
  semantics, not just the canonical mirror. That's deliberate — we're
  fixing the bug at the source rather than burying it in the canonical
  rewrite.

### Option B — bug-compatible canonical

Add `atr=0 → trail=peak` to canonical's `_new_trailing_stop`. Forces
canonical to mirror legacy bit-for-bit, then schedule a Phase-C cutover
that fixes the bug on both sides simultaneously.

Pro:
- Strangler-fig orthodoxy: shadow mode preserves byte-for-byte
  behavior, real semantic changes happen in a separate explicit phase.
- Promotion gates are unaffected during this fix.

Con:
- Propagates a bug into the canonical engine that we'll eventually
  have to remove. Adds a second cutover gate (post-removal) to the
  schedule.
- Documents the wrong intent in canonical (the "Frozen priority"
  docstring at `evaluate_bar`).

### Option C — exempt ATR=0 from the parity verdict

Filter out parity rows whose canonical `reason_code` is the
"trail-update-skipped-because-atr-zero" case. Do nothing to either
engine. Verdict passes; behavior unchanged.

Pro:
- Smallest blast radius; no behavior change anywhere.

Con:
- Hides the issue rather than resolving it. The cutover gate is
  designed to surface exactly this kind of structural disagreement.
  Exempting weakens the gate's value.
- Doesn't fix the underlying legacy bug.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/exit_evaluator.py::_new_trailing_stop` —
  canonical reference for the "skip on atr<=0" pattern.
- `app/services/trading/exit_parity_metric.py::compute_parity_v2_fields`
  — for Option C only; would gain an `is_atr_zero` short-circuit.
- `tests/test_exit_evaluator_parity.py` — existing parity bench;
  add an ATR=0 fixture row for whichever option ships.

## Constraints / do not touch

- **Hard Rule 5 still applies** — prediction-mirror authority is
  frozen. None of these options touch it; this is exit-engine scope.
- **No live-engine semantics change.** Option A and Option B are
  backtest-only changes (legacy) or shadow-only (canonical). Live's
  trail-close is already disabled in canonical
  (`build_config_live` sets `trail_atr_mult=None`); no live
  divergence to chase.
- **Don't ship Option A and a promotion-gate change in the same
  commit.** They need to be observable independently.

## Out of scope

- Live exit-engine cutover (separate brief once gate passes).
- Live-source `INSUFFICIENT_DATA` — that resolves with soak, not code.
- Repricing of any patterns currently `promoted` — Option A may shift
  CPCV/EV scores marginally; if a pattern flips status, that's a
  separate audit.

## Success criteria

For Option A (recommended path):
1. CC modifies `app/services/backtest_service.py:1322-1346` to skip
   the trail-close decision when `atr_val == 0` (mirror canonical).
2. Add a regression test under `tests/` that constructs a bar with
   ATR=0 and asserts both engines return `hold`.
3. Run `pytest tests/test_exit_evaluator_parity.py` — must pass.
4. Restart backtest worker; let it accumulate ≥6h of new parity rows.
5. Re-run the cutover-gate verdict query; backtest source must move
   to `PASS`.
6. Snapshot pre-fix vs post-fix promotion-gate verdicts on the
   currently-promoted patterns; flag any flips for operator review.
7. Commit + push + CC report.

For Option B or C: success criteria differ; CC should re-confirm with
operator before starting if those are chosen.

## Rollback plan

- Option A regression: revert the single-file edit; previously
  spurious closes resume; gate goes back to `FAIL_ASYMMETRIC_AGGRESSIVE`
  (same state as today).
- Option B regression: revert canonical change; gate stays
  `FAIL_ASYMMETRIC_AGGRESSIVE`.
- Option C regression: revert filter; gate stays the same.

No live-trading risk in any direction — this is shadow/backtest only.

## What CC should do if unsure

- If Option A is chosen and ≥1 currently-promoted pattern flips its
  CPCV/EV verdict post-fix, **STOP** and surface to operator before
  committing. That's a promotion-gate side effect that needs a
  separate strategy decision.
- If `_phase_b_bt_shadow_parity` is reading state in a way that
  makes the fix non-local (e.g., needs to skip the parity hook on
  ATR=0 bars), surface back instead of restructuring the harness.
- If the post-fix verdict is `PASS` but the live source is still
  `INSUFFICIENT_DATA`, do NOT recommend cutover — both sources need
  PASS for ≥7 consecutive days before flipping.
