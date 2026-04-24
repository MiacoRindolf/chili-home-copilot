# Roadmap deviation 005 — Q1.T1.7 graded `cpcv_n_paths` (T1.5 follow-up)

## Context

**Q1.T1.5** calibrated purge fraction, embargo fraction, and combinatorial path **budget** to labeled sample size, and introduced **trade-count** tiers: insufficient (&lt;15), **provisional** (15–29 trades, tag `provisional_sample_size`), full (≥30).

The promotion gate still used a single hard floor **`cpcv_n_paths ≥ 50`** for path count. On CHILI-scale histories (e.g. ~158 realized trades), the CV machinery often produces **~20 valid paths** even when DSR, PBO, and median path Sharpe are strong — so patterns were rejected for **procedural path cardinality**, not weak statistics.

## Q1.T1.7 (this change)

- **`chili_cpcv_n_paths_provisional_min`** (default **20**, env `CHILI_CPCV_N_PATHS_PROVISIONAL_MIN`): below this → fail `cpcv_n_paths_below_provisional_min` (floor unchanged; not lowered).
- **`chili_cpcv_n_paths_full_min`** (default **50**, env `CHILI_CPCV_N_PATHS_FULL_MIN`): at or above, with all metric checks passed → full-confidence pass on paths (no path provisional tag).
- Between provisional and full minima, with all metric checks passed → pass with **`provisional_small_paths`** in `promotion_gate_reasons` (parallel to **`provisional_sample_size`** for trade count).

Implementation: [`app/services/trading/promotion_gate.py`](../app/services/trading/promotion_gate.py) (`promotion_gate_passes`). Docs: [`CPCV_PROMOTION_GATE_RUNBOOK.md`](CPCV_PROMOTION_GATE_RUNBOOK.md).

## Migration 170

**`170_restore_pattern_1047_n_paths_threshold_second`** restores pattern **1047** after T1.6 backfill demotion on the old single-tier path rule. **First** restoration was **168** (classifier vs realized-PnL miscalibration). SQL matches **168**: clear CPCV gate columns, `lifecycle_stage = 'promoted'`.

## Shipped migrations

Do not edit **163–169**; **170** appends only.

## Related tech debt

Patterns with **no / thin PTR history** remain skipped by backfill until they accumulate trades (see **Q1.T2.5** in [ROADMAP_DEVIATION_003.md](ROADMAP_DEVIATION_003.md)); T1.7 does not address that orthogonal gap.

## Resolution

T1.7 is deployed on `main`, migration **170** applied on canonical `chili`, and the post-deploy CPCV backfill (`--commit`, exit **0**) left pattern **1047** in **`promoted`** with **`promotion_gate_passed=true`**, **`pattern_evidence_kind=realized_pnl`**, **`cpcv_n_paths=20`**, **`cpcv_median_sharpe≈0.80`**, **`deflated_sharpe=1.0`**, **`pbo=0.0`**, and **`promotion_gate_reasons`** containing **`provisional_small_paths`**. **`CHILI_CPCV_PROMOTION_GATE_ENABLED`** remains **off** (shadow mode).
