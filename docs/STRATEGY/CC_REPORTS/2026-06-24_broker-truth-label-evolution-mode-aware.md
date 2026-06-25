# CC_REPORT: broker-truth-label-evolution-mode-aware

Closes the "Deferred" item from
[2026-06-24_broker-truth-label-route-remaining-learning-consumers.md](2026-06-24_broker-truth-label-route-remaining-learning-consumers.md):
the `evolution.py` (and the discovered `strategy_params.refine_strategy_params`)
learning reads that still trained on the contaminated self-report label. After this,
flag-ON `chili_momentum_broker_truth_label_enabled` gives **fully** clean-label learning.

## The mode-aware problem (why these couldn't use the plain accessor)

These consumers aggregate PAPER + LIVE outcomes together (`paper_vs_live_performance_slices`,
the per-mode viability nudge, variant kill/pause, param refinement). A paper outcome
never gets a `broker_recon_status` (the WRITE pass is live-only), so a naive
`skip is_reconciled=False` would have dropped EVERY paper row under flag-ON — nuking the
paper arm. Paper has no broker truth; its self-report IS its truth.

## What shipped

New shared accessor `outcome_reconcile.mode_aware_label_for_outcome(outcome) -> (return_bps, realized_pnl_usd, usable)`
(lives in outcome_reconcile.py so both evolution.py and strategy_params.py can import it
without a cycle — evolution imports strategy_params):
- **Flag-OFF**: legacy `(return_bps, realized_pnl_usd, True)` for EVERY row — byte-identical.
- **Flag-ON**: paper → legacy self-report (usable); live `reconciled` → broker-true (usable);
  live unreconciled / never-reconciled → `(None, None, False)` (EXCLUDED).

Routed through it:
- `evolution._aggregate_rows` — `n` now counts USED rows; broker-true live + paper self-report; the setup-adjusted channel uses the mode-aware bps.
- `evolution.apply_outcome_feedback_to_viability` — the per-outcome viability nudge tally uses the mode-aware bps.
- `evolution.maybe_pause_symbol_variant_after_losses` — flag-ON conservative: an unreconciled live row in the last 3 cannot confirm the loss streak → no pause.
- `evolution.maybe_kill_underperforming_variant` — KILL decided on broker-true bps; unreconciled live excluded.
- `strategy_params.refine_strategy_params` — param refinement tunes off the mode-aware label; unreconciled live rows drop out of the sample.

## Final sweep — relabel is complete

Swept all `.return_bps` / `.realized_pnl_usd` attribute reads in momentum_neural. The
LEARNING/DECISION consumers are now ALL routed (this commit + PR #821 + mig309). What
remains reads the legacy field by design and is NOT a label trainer:
- `feedback_query._outcome_brief` — display read-model (surfaces broker_* additively, PR #821).
- `brain_desk_summary._weighted_mean_return_bps` — operator desk PREVIEW (display read-model; left on legacy, could be made additive later — flagged, not a gate).
- `evolution.py` ingest audit trace (records the raw self-report — correct).
- `feedback_emit._computed_existing_row_credit` — evolution-credit/provenance recompute (the credit gate decides on packet lineage, not PnL magnitude; broker truth isn't available at credit time).
- `outcome_extract` / `outcome_reconcile` write + source paths.
- `compute_session_evidence_weight` — evidence WEIGHT from the extracted dict at ingestion (broker truth not available at write time; it is weight, not label).

## Verification

`chili_test`, conda `chili-env`, single-process.

- NEW `tests/test_broker_truth_label_mode_aware.py`: **11 passed**. DB-free unit tests for the helper, `_aggregate_rows` (n=used, broker-true + paper self-report), and `refine_strategy_params` (flag-OFF mean +60 vs flag-ON broker-true −60; unreconciled-live → sample_size 0). DB-backed tests for `maybe_kill` (flag-OFF spares, flag-ON KILLS on broker truth), `maybe_pause` (flag-ON conservative no-pause on unreconciled; flag-OFF pauses on 3 legacy losses), and `apply_outcome_feedback_to_viability` (nudge tally uses broker 80 flag-ON vs legacy 50 flag-OFF).
- flag-OFF asserted byte-identical at every site.

## Surprises / deviations

- The earlier full-file run hit a `statement timeout` on the per-test TRUNCATE (~190 tables) for `test_maybe_kill` — pure DB contention (run took 7 min vs the usual ~2.5). Re-running that test alone: **1 passed in 80s**. Not a logic failure; the box/DB was loaded.
- Discovered `strategy_params.refine_strategy_params` (not in the original deferred note) is also a label trainer — routed it for completeness.

## Open questions for Cowork

1. `brain_desk_summary._weighted_mean_return_bps` is a desk preview left on legacy. Want it made additive (show broker-true preview alongside) like feedback_query, or leave as-is?
2. With this landed, flag-ON is fully clean for learning. Ready to soak `chili_momentum_broker_truth_label_enabled=ON` after the WRITE pass (`chili_momentum_broker_truth_reconciliation_enabled`) has populated enough reconciled rows to inspect the divergence distribution?
