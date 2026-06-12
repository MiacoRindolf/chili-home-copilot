# 2026-06-12 — OHLCV integrity gate: volume-confirmed-move veto + quality-reject propagation

**Branch:** `chili/ohlcv-split-detector-volume-veto` (worktree off origin/main 7085f8b)
**Trigger:** operator-direct (selection-alpha mining found the bug 2026-06-12); not from NEXT_TASK.md

## Problem

`validate_ohlcv_integrity` → `detect_stock_split` flagged any ≥45% daily close
ratio near a "common split ratio" (2.0, 0.5, …) as `probable_splits` and the
gate rejected the whole 1d frame. But a real +100% momentum day IS ratio 2.0 —
so DSY, NPT, MTEN, AHMA, FAC, SUNE, SDOT all had their daily frames rejected by
every provider tier. Two compounding bugs:

1. `filter_bad_prints` (z-score, runs first inside `clean_ohlcv`) silently
   DROPPED the +100% bar in quiet frames (z ≫ 5), corrupting gap/RVOL features
   even where validation would have passed.
2. With `allow_provider_fallback=True` (default), the rejected frame's
   `quality_rejected` attrs were discarded when later tiers came back empty —
   callers got a plain empty DataFrame and couldn't distinguish "no data"
   from "rejected". Net: every 1d feature (gap vs prev close, RVOL/adv20,
   day-N continuation, MA distances) went silently null for exactly the
   explosive Ross-lane names the momentum system targets.

## Fix

**`app/services/trading/data_quality.py`** — one adaptive discriminator,
`_is_volume_confirmed_move(df, i)`: bar dollar volume (Close×Volume) vs
`min(trailing-20 median, frame-wide median)`. Dollar volume is invariant to a
split's mechanical share-count change (split day ≈ 1× baseline) while real
hyper-mover days run 50–500×. The min-of-two-baselines handles the
pump-collapse case (live NPT 03-18: −49% the bar after a vertical squeeze —
trailing window IS the pump, frame median still sits at quiet level).
One irreducible base: `_VOLUME_CONFIRMED_MOVE_DOLLAR_RVOL = 5.0` (mirrors the
Ross "explosive" RVOL floor); the comparison itself is per-name adaptive.

- `detect_stock_split`: split-shaped ratios on volume-confirmed bars are not
  flagged.
- `filter_bad_prints`: z-outlier bars that are volume-confirmed are kept
  (true bad prints have no volume expansion and are still dropped).

**`app/services/trading/market_data.py` `fetch_ohlcv_df`** — track
`last_quality_rejected` through the fallback chain; all three terminal
empty-returns now return the rejected frame (with `quality_rejected` /
`quality_issues` / `provider` attrs) instead of a plain empty frame.
Backward compatible: rejected frames are still `.empty`; no existing consumer
reads the attrs (grep-verified). Docstring documents the attrs contract.

## Verification

- 20/20 `tests/test_data_quality_integrity.py` + `test_market_data_dead_cache_fallback.py`
  (7 new regression tests: DSY/NPT-shaped +99% frame not flagged AND bar
  survives `clean_ohlcv`; NPT pump-collapse −49% frame not flagged; forward
  2:1 and reverse 1:10 splits at ~1× dollar turnover STILL flagged; z-outlier
  bad print on flat volume STILL dropped; rejected-attrs propagate when all
  fallbacks empty; hyper-mover frame passes the fetch gate end-to-end).
- 27/27 neighboring `test_market_data.py`, `test_market_data_implausible_guard.py`,
  `test_ohlcv_panel_freshness.py` — no collateral regressions.
- Live spot check via real providers: all 7 reported symbols now return clean
  63-row massive frames with moves intact — NPT +284% (was rejected),
  DSY +291% (was rejected), SUNE +420%, SDOT +108%, MTEN +81%, AHMA +66%,
  FAC 5 rows (fresh listing, real).

## Notes / follow-ups

- The mining-time workaround in `scripts/_alpha_build_matrix.py` (night-ops
  worktree) that called `massive_client.get_aggregates_df` raw can be removed
  once that branch rebases onto this.
- An explicit `integrity='warn'` fetch mode (candidate 3 in the bug report)
  was deliberately NOT added: the volume veto removes the need, and an unused
  opt-out knob is a dark flag.
- Residual risk accepted: a true split on a name trading sustained-explosive
  dollar volume with no expansion on split day would be missed; perfect
  discrimination needs a corporate-actions endpoint check (future candidate).
