# CC_REPORT: broker-truth-label-route-remaining-learning-consumers

Follow-up to mig309 (`outcome_reconcile.authoritative_label_for_outcome`). Routes the
remaining momentum-lane learning/decision consumers that still read
`MomentumAutomationOutcome.return_bps` / `realized_pnl_usd` DIRECTLY through the single
label accessor, so flipping `chili_momentum_broker_truth_label_enabled=ON` gives
clean broker-true learning instead of a partial relabel.

## What shipped

Branch `chili/momentum-broker-truth-label-followup` off `chili/momentum-defensive-veto-bundle`
(the branch that carries mig309 — it is NOT in `main` yet, so this stacks on it).

Routed through `authoritative_label_for_outcome` (flag-OFF byte-identical; flag-ON uses
the broker-true `return_bps` for `reconciled` rows and SKIPS `is_reconciled=False`,
mirroring the meta_label / risk_evaluator precedent):

- `family_regime_stats.aggregate_family_regime_performance` — feeds the family×regime arming prefilter gate *(named in brief)*
- `ab_test.compare_peer_variants._slice` — the A/B winner decision; now loads full ORM rows so the accessor can read the `broker_*` columns *(named in brief)*
- `viability._symbol_family_memory_adjust` — the symbol×family viability boost/penalty *(NOT named in brief; it is the exact same "track-record → bias" shape as family_regime_stats, so routing one and not the other would have left a partial relabel)*

Treated additively (NOT route-and-skip) — read-model:

- `feedback_query._outcome_brief` — now surfaces the raw `broker_recon_status` / `broker_realized_pnl_usd` / `broker_return_bps` / `broker_divergence_usd` ALONGSIDE the untouched legacy fields, flag-INDEPENDENT. A desk read-model must keep the lane-vs-broker divergence VISIBLE (that is what the operator inspects before flipping the flag); dropping unreconciled rows here would hide the very divergence mig309 exists to surface.

Already covered (no change needed):

- The self-critic (`meta_label._compute_diagnostics` / `_propose_research_agenda`) trains off `load_training_rows`, which mig309 already routed — so it is transitively clean.

Files touched: 4 modified + 1 new test file + this report.
Migrations added: none (label columns + accessor are mig309).

## Verification

`TEST_DATABASE_URL=...chili_test`, conda `chili-env`, single-process runs (truncating fixture).

- NEW `tests/test_broker_truth_label_consumers.py`: **8 passed** — for each routed consumer, flag-OFF byte-identical (paper+live counted, no row dropped) AND flag-ON broker-true-only (unreconciled/paper excluded; proven by a win-rate / mean / A-vs-B-winner / viability-sign FLIP). Plus the additive-brief / never-hide assertions.
- `tests/test_broker_truth_reconcile.py`: **7 passed** — accessor + WRITE pass unaffected.
- `tests/test_trading_decision_stack.py`: **43 passed, 3 failed**. The `_outcome_brief` consumer test passed. The 3 failures (`test_live_tick_*`, KeyError at line 1510) are **PRE-EXISTING**: they fail identically on pristine bundle HEAD with my edits `git stash`-ed away (verified). Unrelated to label routing — they exercise the live-tick → place_market_order path my change does not touch.

## Surprises / deviations

- **The task's "remaining readers" list was incomplete.** Besides the named family_regime_stats / ab_test / feedback_query, `viability._symbol_family_memory_adjust` is a genuine same-shape learning read (routed here), and `evolution.py` has SEVERAL more (see Deferred).
- **feedback_query is a read-model, not a trainer** — routed additively rather than route-and-skip (rationale above). Flagging the deviation per the "flag, don't drift" rule.
- **Flag-ON drops paper rows** from the routed mode-mixing aggregates (paper outcomes never get a `broker_recon_status`). This is the accepted accuracy-over-quantity contract (same as meta_label / risk_evaluator) and is documented inline. It is a trading-behavior change gated entirely behind the default-OFF READ flag; flag-OFF is byte-identical.

## Deferred (needs a planner decision — NOT done here)

`evolution.py` has additional UNROUTED direct learning/decision reads of the contaminated label:
`_aggregate_rows` (via `aggregate_recent_outcomes_for_variant` / `paper_vs_live_performance_slices`),
`apply_outcome_feedback_to_viability` (per-outcome viability nudge), `maybe_pause_symbol_variant_after_losses`,
and `maybe_kill_underperforming_variant`. These are **mode-separated** — `paper_vs_live_performance_slices`
compares paper vs live, and the viability nudge buckets paper/live separately — so a naive
`skip is_reconciled=False` would NUKE the paper arm under flag-ON. They need MODE-AWARE routing
(route the live arm through the accessor; keep paper on its self-report, which IS paper's truth),
which deviates from the task's stated skip pattern and is a design call for Cowork. Until that lands,
flag-ON learning is broker-true for selection/A-B/viability-memory/daily-loss/meta-label but still
self-report for the evolution variant-kill / pause / per-mode viability nudge.

## Open questions for Cowork

1. Approve the mode-aware evolution.py follow-up (route live arm, keep paper on self-report)? That closes the last contaminated-label learning reads before relying on the flag.
2. Confirm the feedback_query additive treatment (surface `broker_*` on the desk, never hide) is the desired UX for the pre-flip divergence inspection.
