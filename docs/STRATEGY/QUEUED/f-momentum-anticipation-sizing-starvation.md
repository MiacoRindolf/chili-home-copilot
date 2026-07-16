# f-momentum-anticipation-sizing-starvation

STATUS: QUEUED
SLUG: momentum-anticipation-sizing-starvation
PRIORITY: P0 AFTER CURRENT MAIN-THREAD TASK
PROPOSED: 2026-06-30
REQUESTED_BY: operator sizing audit after CUPR/GVH/PMN/LGPS live fills
SCOPE: momentum lane live sizing, anticipation starter, A-setup floor, replay coverage

## TL;DR

Recent live equity fills are small because CHILI is self-throttling:

- CUPR session 10013 entered about $27 and about $21 notional.
- GVH session 10044 entered about $29 notional.
- PMN session 10048 entered about $182 notional.
- LGPS session 10125 entered about $51 notional.

This is not a broker partial-fill problem. The live runner intentionally submitted the small probe quantities. The current deployed design can turn "probe then add remainder" into "probe only" because the remainder only submits after a strict green confirmation, currently `bid > avg_entry_price`. In the audited 8-hour window, there were zero `live_anticipation_remainder_submitted` and zero `live_anticipation_remainder_filled` events.

Expected by code, not acceptable as the target Ross-style autonomous momentum behavior.

## Live Evidence

Deployed scheduler at audit time:

```text
chili-app:main-clean-cb53d54
started 2026-06-30T23:31:32Z
```

Broker-confirmed entries in the audited window:

```text
CUPR 10013 qty 4 @ 6.78    notional 27.12
CUPR 10013 qty 3 @ 7.1199  notional 21.36
GVH  10044 qty 6 @ 4.7799  notional 28.68
PMN  10048 qty 14 @ 13.0299 notional 182.42
LGPS 10125 qty 44 @ 1.165  notional 51.26
```

Important event facts:

```text
CUPR 10013 live_anticipation_probe_sized full_qty=16 probe_qty=4 remainder_qty=12
CUPR 10013 live_anticipation_probe_sized full_qty=14 probe_qty=3 remainder_qty=11
GVH  10044 live_anticipation_probe_sized full_qty=26 probe_qty=6 remainder_qty=20
PMN  10048 live_anticipation_probe_sized full_qty=58 probe_qty=14 remainder_qty=44
LGPS 10125 live_anticipation_probe_sized full_qty=178 probe_qty=44 remainder_qty=134
```

Across the last 8 hours at audit time:

```text
live_anticipation_remainder_submitted = 0
live_anticipation_remainder_filled = 0
```

LGPS specific:

```text
trigger_reason = ma_vwap_pullback
pre_floor_loss_usd = 11.8
adaptive floor_loss_usd = 69.71
a_setup_size_floor_eval.applied = false
a_setup_size_floor_eval.reason = hard_blocker
hard_blockers = {"extreme_vol": 0.5}
risk-first full_qty = 178
submitted probe_qty = 44
remainder_qty = 134
```

PMN specific:

```text
a_setup_size_floor_eval.applied = true
pre_floor_loss_usd = 7.39
post_floor_loss_usd = 60.70
full_qty = 58
submitted probe_qty = 14
remainder_qty = 44
```

So the A-setup floor can work, but anticipation still downshifts the actual live exposure to the starter leg unless the add path fires.

## Root Causes To Fix

1. Anticipation starter can starve final size.

The starter fraction is currently a fixed config default of 0.25. That can be valid only if the remainder reliably follows on confirmed continuation. Live evidence says it did not follow in the audited window.

2. Remainder trigger is too brittle.

The deployed code requires the position to be green with `bid > avg_entry_price`. That misses valid Ross-style pullback/reclaim cases where the setup confirms by holding pivot/VWAP/micro structure before bid is above the fill, or where spread makes bid lag the real tape.

3. `extreme_vol` is acting as a floor hard blocker.

If viability admits an explosive Ross-class name as tradable, `extreme_vol` should usually become an adaptive risk cap or derate, not an automatic blocker that prevents A-setup floor rescue. Hard blockers should remain for truly toxic cases: halt-chain, severe L2/liquidity, red-day breaker, governance, notional cap, stale/fake data, or stop/daily-loss protection.

4. Telemetry does not make missed remainder obvious enough.

The system records `anticipation_remainder_qty`, but it needs explicit skip telemetry per tick or per state transition:

```text
live_anticipation_remainder_wait
reason = not_green_bid | stale_quote | spread_too_wide | no_tape | no_micro_hold | in_flight | disabled
bid, ask, avg_entry, vwap, pivot, micro_frame_used, tape_score, ofi_level, ofi_slope
```

## Required Design Direction

Do not replace this with another fixed magic-number rule. Make the sizing and add logic adaptive.

Recommended model:

1. Compute intended full risk using existing risk-first sizing and A-setup floor.

2. Choose starter fraction adaptively from live structure quality:

```text
starter_fraction = f(
  spread_cost_vs_expected_R,
  microbar confirmation quality,
  tape/OFI freshness and direction,
  volatility regime,
  liquidity/depth quality,
  distance to pivot/VWAP/structural stop,
  frontside strength,
  replay-calibrated expectancy bucket
)
```

The fraction should be bounded by policy, but the bound should come from replay-calibrated percentiles or account-risk policy, not hidden constants.

3. Remainder should fire from event-driven confirmation, not only `bid > avg_entry_price`.

Valid confirmation examples:

```text
tick-level break above pullback high or shelf
microbar close holding VWAP/pivot
tape thrust after probe fill
OFI slope improving with fresh quote
spread cost acceptable relative to expected R
no stale NBBO/depth conflict
```

4. `extreme_vol` should be converted from floor hard blocker into adaptive cap unless paired with toxic evidence.

Examples:

```text
extreme_vol + healthy tape + fresh quote + acceptable spread/R = cap risk, do not block floor
extreme_vol + stale quote/depth + toxic spread + weak tape = hard cap or no remainder
```

5. Preserve true protections.

Do not weaken:

```text
max-loss circuit
daily loss breaker
portfolio drawdown breaker
halt/extreme halt chain safety
severe L2/liquidity protection
stale/fake quote guards
notional cap
stop protection
```

## Replay And QA Bar

Before deploy, require replay coverage that proves the full behavior, not just entry eligibility:

1. Replay case: A-setup soft derates would crush risk, floor lifts it, full risk-first qty is computed.

2. Replay case: anticipation starter submits probe, then event-driven confirmation submits the remainder.

3. Replay case: valid pullback/reclaim confirms by VWAP/pivot/microbar hold even if bid is not immediately above avg entry.

4. Replay case: extreme volatility with clean tape caps risk adaptively but does not block the A-setup floor.

5. Replay case: toxic spread/stale quote/severe L2 still prevents aggressive remainder or caps risk.

6. Replay PnL harness should compare:

```text
current probe-only behavior
adaptive starter + event-driven remainder
full-size single entry
```

The pass criterion should not be only "tests green." It should show that adaptive sizing improves or at least does not degrade replay expectancy and max drawdown under realistic friction assumptions. If Replay v3 cannot reliably answer that yet, upgrade replay first and mark this item blocked by replay fidelity.

## Acceptance Criteria

- Recent-size audit shows no qualified A-setup remains stuck at probe-only without an explicit telemetry reason.
- `live_anticipation_remainder_submitted` appears in replay for valid confirmed scenarios.
- Live telemetry has enough fields to explain every missed remainder.
- No new fixed magic-number thresholds are introduced without being settings-backed, percentile-derived, or replay-calibrated.
- Tests cover CUPR/GVH-style soft-derate crush, PMN-style floor success, and LGPS-style extreme-vol/pullback probe starvation.

