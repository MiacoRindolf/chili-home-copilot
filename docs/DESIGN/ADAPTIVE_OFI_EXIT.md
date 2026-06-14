# Adaptive order-flow exhaustion lock (crypto momentum exit)

Status: SHIPPED (Action A ratchet live-on; Action B partial behind a flag,
log-would-fire-first). Crypto-only; equity exit byte-identical.

## The flaw it fixes

On MEGA-USD the runner reached `STATE_LIVE_TRAILING` via trail-activate
(`live_runner.py:3896`) but never took the first-target partial — `bid` never
hit the **3R** crypto target (`bid >= target_px*0.995`), so `partial_taken`
stayed `False`, `_be_floor` stayed at the **loss-side stop** (`live_runner.py:3616`),
and the runner gave back a +1.9R peak inside the ~815bps cushion band that never
triggered. The fixed 3R target was too far; the cushion band was too loose at
that cushion level. There was no *flow-driven* reason to de-risk before the
target.

The fix: an **adaptive, order-flow-confirmed lock** that, on an armed winner,
(A) ratchets the runner stop tighter and (B optionally) arms the early partial
the moment live OFI + micro-price say the thrust is exhausting — **before** the
fixed target. It is the sign-mirror of the entry OFI tilt (`viability.py:174-189`).

## Surface

| Where | What |
|---|---|
| `paper_execution.py` `ofi_exhaustion_lock(...)` | Pure, ratchet-only decision helper. Returns `new_stop_floor`, `partial_arm`, and the **A/B counterfactual** (fixed-R:R stop, lock OFF). |
| `live_runner.py` (primary hook, in the TRAILING block, AFTER the cushion ratchet) | Crypto-gated. Reads `_live_ofi_microprice`, computes `current_band_bps` from the band's REALIZED stop this tick, calls the helper, applies Action A via the existing `> stop_px` ratchet, sets the one-tick `exhaustion_lock_partial_armed` flag, and **emits `live_ofi_exhaustion_lock` on every armed tick** (the counterfactual). |
| `live_runner.py:3778` (secondary hook) | OR-s `exhaustion_lock_partial_armed` into the partial trigger — gated **directly** on `-USD` and on `not scale_limit_order_id`, parenthesized so the state/`partial_taken`/resting-limit guards still apply. |
| `config.py` | 5 new `chili_momentum_exit_ofi_*` knobs (below) + reuses `chili_momentum_ofi_threshold` and `chili_crypto_l2_ofi_window_s`. |

## The trigger (confluence-AND)

Fires only when ALL hold (the precise sign-mirror of the entry tilt):

1. **Profit-arm** — `peak_r >= arm_r` where `arm_r = arm_frac * rr` (derived from
   the plan's own reward:risk, floored 0.5R). Below the arm the lock is **inert** —
   a healthy early pullback is owned entirely by the trail/structural stop. This
   is the dominant false-positive killer (failure mode 1).
2. **Micro-price rollover** — `micro_edge < 0` (the spoof-resistant *state* anchor).
3. **OFI flip** — `ofi < -T` over the 15s ring window (`T = chili_momentum_ofi_threshold`,
   the SAME tuned constant entry uses). Demoted to confirmation — never fires alone.
4. **Giveback corroborant** — `(hwm - bid) >= k * risk_dist` where `k` derives from
   the position's own ATR risk unit and scales DOWN as the move extends. No fixed bps.

**Accelerant (OR-bypass of 3+4):** hidden-seller absorption at the highs
(`_hidden_seller >= 1.0` with micro rollover) arms on gates 1+2 alone — absorption
is the one *leading* distribution signal. **Off by default**
(`chili_momentum_exit_ofi_hidden_seller_enabled=false`) — log-only-first; the
profit-arm gate is still required (it cannot cause a loss-side sell).

**Graceful degradation:** if the ring is stale/empty, `_live_ofi_microprice`
returns `(None, None)`, the helper no-ops, and control falls through to the
existing cushion trail + `bid <= stop_px` enforcement. The flow exit never goes
blind — it only ever *adds* a tightening.

## How it ADAPTS (≤1 documented knob)

- **`base_lock_bps` is the ONE irreducible number.** Everything else is derived:
  - `lock_bps = clamp(base * strength_scale * flow_scale, floor=0.25*base, ceil=current_band_bps)`
  - `strength_scale` tightens with the move's percentile within its own plan
    (`peak_r/rr`) — more to protect, less expected continuation.
  - `flow_scale` tightens with `|ofi|` beyond `T` and `|micro_edge|` (and absorption).
  - The **ceiling is the cushion band's REALIZED stop this tick**, so the lock can
    only ever EQUAL or TIGHTEN the trail, never widen it.
- `arm_r` derives from `rr`; the giveback arm derives from `risk_dist`. No fixed-R
  or fixed-bps magic beyond `base_lock_bps`.

## Action A vs Action B

- **Action A (ratchet-tighten)** — live-on by default. Raises the runner stop
  toward the high-water mark; purely additive over the structural stop.
- **Action B (early partial)** — behind `chili_momentum_exit_ofi_lock_partial_enabled`
  (default OFF, log-would-fire-first). When armed it routes through the **existing
  audited** `STATE_LIVE_SCALING_OUT -> _apply_confirmed_live_partial_exit ->
  breakeven_stop_after_partial` path, which flips `_be_floor` to breakeven — the
  exact MEGA give-back fix. The scale-out **fraction reuses** the tuned
  `scale_out_fraction`; only the *trigger* is new.

Both AUGMENT the fixed target — the frozen 3R/2R `target_px` path is untouched.
The position now de-risks on `target_px` reached **OR** flow-confirmed exhaustion
past the arm: strictly a superset of today's behavior.

## Safety invariants (verified by tests)

- **A — never loosen the floor.** The helper returns `max(current_stop,
  breakeven_floor, candidate)` UNCONDITIONALLY, and the caller re-applies its own
  `> stop_px` ratchet (belt-and-suspenders). Holds for any input incl NaN /
  negative bps / hwm < entry. The `bid <= stop_px` market exit is never gated.
- **B — partial accounting through the chokepoint.** Action B never books PnL
  inline; it goes through `_apply_confirmed_live_partial_exit` (fee leg-local,
  entry fee booked once at full exit) and the economic ledger.
- **C — no double-exit.** The armed partial honors `not scale_limit_order_id`
  (the resting-limit path owns the level that tick) and `partial_taken` (fires
  once); every broker sell still funnels through `_cancel_scale_limit_and_clamp`.
- **D — instant revert.** `CHILI_MOMENTUM_EXIT_OFI_LOCK_ENABLED=false` is the
  authoritative kill (whole branch skipped; exact legacy cushion trail + fixed
  target). Band-collapse (floor==ceiling) + topping-tail off reverts the legacy
  trail. Kill-switch / operator FLATTEN flatten through the standard chokepoint.

## Equity byte-identical

The entire lock is gated `sess.symbol.endswith("-USD")` at **both** hooks
(directly, not transitively via the flag). Equities never enter the branch; a
forged `exhaustion_lock_partial_armed` flag in an equity `le` is provably ignored
(test `test_equity_armed_flag_is_byte_identical_no_ofi_partial`).

## How we MEASURE it live (the A/B / counterfactual)

Per the red-team: an unvalidated exit signal can sell a winner early, and the
project's own data shows the system already captures only 0.44–0.67 of winners.
So we PROVE capture before trusting it:

- On **every armed tick** the primary hook emits `live_ofi_exhaustion_lock` with
  BOTH the `adaptive_stop` (lock applied) AND the `counterfactual_fixed_stop`
  (the cushion band's stop, lock OFF) on the same tape, plus `peak_r`, `ofi`,
  `micro_edge`, `hidden_seller`, `lock_bps`, `band_bps`, `fired`, `partial_arm`.
- The realized-PnL delta of adaptive-exit vs the fixed-R:R baseline is then
  accumulated per symbol-session from these events. **Promotion criterion**:
  `delta >= 0` over the soak. The replay_v2 / paper_runner parity siblings make
  this cheap — run the identical helper lock-on vs lock-off over the same
  recorded tape and diff realized PnL.
- **Sequencing consistent with "LIVE not log-gated":** Action A (ratchet-only
  over the structural stop — it can *never* loosen, so the downside of a false
  positive is bounded to capping a continuation, never a loss) ships live-on with
  the counterfactual logging beside it. Action B (which moves size and arms
  breakeven) ships log-would-fire-first behind its flag; promote once the logged
  counterfactual shows the early partial is net-positive (watch especially that
  breakeven-after-a-false-partial does not flush runners net-negative — if it
  does, raise `arm_frac`).

## Config knobs

| Env | Default | Role |
|---|---|---|
| `CHILI_MOMENTUM_EXIT_OFI_LOCK_ENABLED` | `true` | master gate (kill-switch #1) |
| `CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED` | `false` | Action B live vs log-would-fire |
| `CHILI_MOMENTUM_EXIT_OFI_ARM_FRAC` | `0.5` | profit-arm as a fraction of `rr` (derived) |
| `CHILI_MOMENTUM_EXIT_OFI_BASE_LOCK_BPS` | `120` | the ONE irreducible lock-tightness base |
| `CHILI_MOMENTUM_EXIT_OFI_HIDDEN_SELLER_ENABLED` | `false` | absorption accelerant (promote after OFI proves out) |
| (reused) `CHILI_MOMENTUM_OFI_THRESHOLD` | `0.25` | shared entry/exit OFI sign threshold |
| (reused) `CHILI_CRYPTO_L2_OFI_WINDOW_S` | `15` | shared ring window |

## Honesty caveats (carried from the profitability research)

- OFI alone is noise — confirmation-only here; micro-price rollover is the robust
  anchor and the profit-arm gate kills the dominant early-sell failure mode.
- Gap/halt-down is structurally NOT an L2 problem — owned by the breakeven ratchet
  + structural stop + halt standdown. The lock only optimizes the continuous-tape
  portion and degrades to the price stop when the ring is stale.
- Evidence base is thin (n=6 live + 1 replay) — thresholds are priors to refit
  from CHILI's stored snapshots + backfilled forward returns. That is exactly why
  Action B and the hidden-seller override ship log-would-fire-first.
