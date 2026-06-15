# CC_REPORT: atr-swing-stop-distance-guard

Direct operator brief (not a Cowork `NEXT_TASK`). Position-identity Phase 5I
soak is unaffected.

## Problem (live evidence)

`trading_trades` id=2338, **QTEX**, autotrader pattern lane (pattern_id=585):

| field | value |
|---|---|
| entry_price | $2.16 |
| quantity | 1167 |
| stop_loss | **$0.3746 (−82.7% from entry)** |
| stop_model | `atr_swing` |
| take_profit | $2.96 |
| risk on the position | **~$2,084** (≈whole position) |

QTEX is a low-float runner that based near ~$0.40 and ran to a $2.60 high before
fading. The stop sat 82.7% below a $2.16 momentum entry — risking the entire
position with effectively no protection.

### Root cause (confirmed against the live DB + code)

1. Trade 2338 was reconstructed by **broker-sync** (`management_scope=broker_sync`),
   which calls `_compute_trade_snapshot('QTEX', 2.16)` →
   `scanner._score_ticker` (the **daily** swing scorer, `interval="1d"`).
2. On QTEX's runner day the **daily ATR ≈ $0.71** (≈33% of the $2.16 price,
   because the base→peak day had a ~$2.20 true range).
3. `_long_atr_trade_levels(price=2.16, atr≈0.71, stop_mult=2.5)` =
   `2.16 − 2.5×0.71 ≈ $0.375`. The math reproduces the live stop exactly.
4. `atr_swing` is merely the default equity stop-model **label**
   (`broker_service.py:2268`), applied to every equity position regardless of
   whether the setup is a swing or a momentum/runner.

The existing geometry guard `scanner._max_atr_fraction_for_levels` bounds the
**ATR/price ratio** (0.33 < 0.35 → passes) but never the resulting **stop
distance**. With `stop_mult` up to 2.5, the prior code allowed stops up to
**~87% below entry**. That is the gap: *a swing/position-trade daily-ATR stop
was applied to a momentum entry, and nothing capped the stop distance.*

## What shipped

A single shared **stop-distance guard** wired into the two ATR-geometry
chokepoints. It caps the stop **distance** at an adaptive, env-overridable,
kill-switchable fraction of entry — tightening (never widening) the stop and
**scaling the target by the same factor so reward:risk is preserved**.

- **New** `app/services/trading/stop_distance_guard.py`
  - `max_stop_distance_fraction(crypto)` — env knob
    `CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK` (default **0.30**) /
    `..._CRYPTO` (default **0.35**). Set ≥ 2.0 to disable (reversible
    kill-switch). Mirrors the existing `_max_atr_fraction_for_levels` pattern.
  - `bound_stop_distance(entry, stop, target, is_long, crypto, context)` —
    clamps a long/short stop, scales the target, logs **loudly** at WARNING
    (`[stop_distance_guard] CLAMPED base-anchored stop: …`).
- **`scanner._long_atr_trade_levels`** — the alert/snapshot geometry, used by all
  four ATR scorers (swing, intraday, breakout, crypto-breakout). This is where
  QTEX's $0.3746 was born.
- **`stop_engine._compute_initial_stop`** — the live-bracket geometry (momentum
  lane, fast-path executor, `bracket_intent`).
- **Tests** `tests/test_stop_distance_guard.py` (15) — QTEX clamp, dollar-risk
  reduction, R:R preservation, normal-swing pass-through, boundary, short
  symmetry, crypto ceiling, env kill-switch + tighten, bad-input no-ops, and
  both integration chokepoints.

### Why a fixed documented ceiling (not percentile-derived)

This is a **safety bound**. A percentile derived from the same distribution that
contains the pathological tail would be *raised* by that tail; data-derived risk
bounds have spiked catastrophically before (MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT
§ME-1, per-broker-loss incident). It is the single documented knob the
"adaptive, no-magic" policy permits as the irreducible base, and it mirrors the
existing `_max_atr_fraction_for_levels` env-knob.

### Calibration (60d of real trades, regression-avoidance)

Long stop-distance fraction distribution:

| class | n | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| equity | 229 | 0.127 | 0.268 | 0.555 | 0.820 | 0.962 |
| crypto | 339 | 0.112 | 0.282 | 0.327 | 0.594 | 0.770 |

The legitimate swing bulk sits ≤ p90 (~0.27); the catastrophe is the >0.30 tail
(20/229 equity, QTEX at 0.83). Defaults **0.30 / 0.35** sit just above p90/p95 —
they clamp the pathological tail **without clipping legitimate swing setups**
(parity preserved for the common case).

## Verification

- `tests/test_stop_distance_guard.py`: **15 passed**.
- Existing bracket/stop-engine parity: `test_bracket_intent_compute.py` **passed**
  (normal cases byte-identical — guard is a no-op below the ceiling).
- Live guard applied to trade 2338: orig stop $0.3746 (82.7%, **$2,084** risk) →
  new stop $1.512 (30.0%, **$756** risk) — **$1,327 less at risk**; target
  $2.96 → $2.45 (R:R preserved).
- Trade 2338 is now **closed** (current QTEX ≈ $2.11, near entry) — no live
  broker-stop remediation needed; the fix prevents recurrence.

## Surprises / deviations

- The catastrophic stop was not a literal "swing-low anchor" branch — it is
  `entry − mult×ATR` with a daily ATR that is ~33% of price. Same operator
  diagnosis (base-anchored, multi-day geometry on a momentum entry), different
  mechanical surface. The fix bounds the **distance**, which covers both framings.
- Trade 2338's *row* was created by broker-sync re-scoring, not the autotrader
  open path. The guard sits at the geometry source, so it covers every consumer
  (autotrader alert, broker-sync snapshot, live bracket).

## Deferred / flagged for Cowork

1. **Deeper fix (intraday anchoring).** The guard caps risk but does not change
   that broker-sync recomputes **daily swing** geometry for **momentum**
   positions. A lane-aware stop (anchor to recent intraday structure / the
   entry's own pullback low) is the deeper redesign — larger blast radius,
   recommend a separate slice.
2. **Tighter momentum ceiling.** 0.30/0.35 are catastrophe-catching ceilings for
   the *shared* geometry. If the operator wants Ross-tight momentum stops, set
   `CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK=0.15` (live, reversible) once
   confirmed it doesn't clip legitimate swings.
3. **16:03 ET entry note.** The `entry_date` 20:03 UTC is the broker-sync
   reconstruction timestamp, not necessarily the original fill. Confirming the
   pattern lane's behavior of opening a *faded runner* near the close needs the
   original Robinhood order timestamp (secondary).

## Rollback

`git revert` of the commit, or set the env knob ≥ 2.0 to disable the guard live
(reverts to legacy unbounded geometry). No schema change, no migration.
