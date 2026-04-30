# Response to third-party audit (2026-04-30)

External assessment received late on 2026-04-30. This document re-evaluates
the audit's claims against actual code state (post-R20–R26) and proposes a
phased execution plan.

## Re-evaluation: which claims are still accurate?

**All major claims are confirmed.**

| Audit claim | Current state | Source |
|---|---|---|
| Phase F execution realism is **shadow-only** | TRUE — `brain_execution_cost_mode = "shadow"`, `brain_venue_truth_mode = "shadow"` | `app/config.py:322,329` |
| Phase H position sizer is **shadow-only** | TRUE — `brain_position_sizer_mode = "shadow"` | `app/config.py:360` |
| Phase I risk dial + capital reweight are **shadow-only** | TRUE — both flags shadow | `app/config.py:373,461` |
| Triple-barrier labeling does **not** drive promotion | TRUE — `brain_triple_barrier_mode = "shadow"`; the labeler populates `trading_triple_barrier_labels` but is not consumed by `promotion_gate.py` | `app/config.py:307`, `docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md` |
| CPCV gate is **off by default** | TRUE — `chili_cpcv_promotion_gate_enabled = False` | `app/config.py:862` |
| Active learner uses simple `future_return_5d > 1%` label | TRUE — `_LABEL_THRESHOLD_PCT = 1.0` in `pattern_ml.py:437` | `app/services/trading/pattern_ml.py:437,450` |
| Pattern-trade feature schema v1 is sparse (no regime/sector/SPY/earnings) | TRUE — schema explicitly says future versions may add those | `docs/pattern_trade_features_v1.md` |
| 27 of 28 promoted patterns had no CPCV evidence (skipped CPCV) | WAS TRUE at the audit baseline. Mitigated partially — R22 mig 213 demoted 4 negative-EV patterns; R10's mig 197 + 199 promoted on backtest evidence created the asymmetry; pattern population is currently smaller than at baseline | `docs/TECH_DEBT.md` T2.5; memory `project_pattern_demote_2026_04_27.md` |
| Survivorship bias acknowledged | TRUE — `docs/DATA_SURVIVORSHIP_BIAS.md` exists | repo |
| Robinhood broker layer has order-confirmation + partial-fill gaps | PARTIALLY ADDRESSED — R23 added stop-loss primitive with state machine integration; R26 added defer-on-rejection cooldown; partial-fill correctness + retry/backoff still open | `docs/BROKER_EXECUTION_AUDIT.md`, R23/R26 |
| Strategy concentration on momentum/breakout | TRUE — domain rule explicitly Ross-Cameron-style focus | `.cursor/rules/chili-trading-domain-knowledge.mdc` |

**One claim the audit understates:** since the audit was written, R23 flipped
`brain_live_brackets_mode = "authoritative"` in production for the first
shadow→authoritative cutover of any of these phases. The activation pattern
is now proven (5 distinct bugs surfaced and were fixed cleanly across the
flip; system is stable in authoritative mode). That removes activation as
unknown-territory risk.

## What the audit got right that I underweighted

The 2026-04-30 audit (mine, internal) listed CRITICAL items as discrete
bugs (logger UnboundLocalError, missing CHECK constraints, etc) and worked
through them tactically. That cleared real fires but did NOT address the
audit's central diagnosis: **the controls that should govern live capital
are mostly shadow-only**. R23 was a step toward live-authoritative
controls, but it was *one* of ~14 phase-rollout flags that all default to
shadow. The third-party audit's phrasing — "much stronger on research
instrumentation than on authoritative profit-making machinery" — matches
the structural picture.

## Plan

Five phases, each gated by stability of the previous. No phase should
ship without the previous one being proven stable for at least the
indicated dwell time.

### Phase 0 — Stabilize R23 activation (this week)

**Goal:** confirm Phase G.2 bracket reconciliation continues to operate
cleanly in `mode=authoritative`, gather operational signal before flipping
more flags.

* Watch `g2_*` execution events count (target: 1 placement per
  missing-stop classification, no duplicate fires).
* Watch `monitor_exit_rejected` decisions (target: 95%+ reduction from
  the 1053+227+41/24h baseline post-R26).
* Watch `brain_batch_jobs` running-count (target: stays <5 sustained
  thanks to R25 reconciler).
* Open one open RH trade is currently the test bed (ADT). Add observation
  protocol when next entry fires.

**Exit criterion:** 7 days of clean operation in authoritative bracket
mode, with a written log of any incidents.

### Phase 1 — Activate Q2 Group 1 flags (1–2 weeks)

The Q2 flags 1, 2, 3 (pattern_survival_classifier, perps_lane,
strategy_parameter_learning) are **read-only or feature-store only** and
gated behind their own kill switches. Flag 1 is the prerequisite for any
of the K Phase 3 consumers (sizing, demote, promote_gate) — flipping it
ON starts the daily 03:30 PT feature-collection job and the weekly Sun
04:30 PT training job, neither of which touches trading decisions.

**Activate in order:**

1. `chili_pattern_survival_classifier_enabled` — feature collection
   begins, ~30 days before the model has enough training data.
2. `chili_strategy_parameter_learning_enabled` — bounded threshold
   adaptation; risk is low because the bounds are defined at the
   parameter level.
3. `chili_perps_lane_enabled` — read-only ingestion only; perps live
   trading stays gated by `chili_perps_lane_live`.

**Exit criterion:** ≥30 daily survival-feature snapshots accumulated;
parameter-learning has produced at least one non-trivial threshold update
that the operator agrees with.

### Phase 2 — Promote Phase F (execution realism) shadow→compare (3–6 weeks)

The audit's highest-leverage recommendation. Phase F (`brain_execution_cost_mode`,
`brain_venue_truth_mode`) writes execution-cost estimates and venue-truth logs
in shadow. Promoting to "compare" means the autotrader compares the modeled
cost against the realized cost on every fill, surfacing divergence — but does
NOT yet gate on the model.

**Steps:**

1. Verify `trading_execution_cost_estimates` has rolling spread/slippage
   data for the symbols actively traded.
2. Verify `trading_venue_truth_log` is being populated by paper trades.
3. Audit the `compare`-mode behavior in `execution_cost_model.py`:
   does it just log divergence, or does it also write an alert?
4. Flip `BRAIN_EXECUTION_COST_MODE=compare` first (logs only); after 14
   days of clean divergence, flip `BRAIN_VENUE_TRUTH_MODE=compare`.
5. **Authoritative-for-paper** comes after another 14 days clean: flip
   to `authoritative` for paper trades only, gating new paper trades on
   modeled cost.
6. **Authoritative-for-live** stays gated on operator decision; needs
   30+ days of paper authoritative behavior with no anomalies before
   flipping live.

**Exit criterion:** Phase F authoritative for paper; cost-blind paper
entries blocked for 14 days with measurable expectancy improvement.

### Phase 3 — Promote Phase H (position sizer) shadow→compare→authoritative-paper (4–8 weeks)

Same pattern as Phase 2. The Kelly-aware, cost-corrected, portfolio-aware
sizer is built and writes to `trading_position_sizer_log` in shadow.
Compare mode would flag divergence between sizer-recommended and actual
filled qty; authoritative-for-paper would route paper trades through the
sizer.

**Prerequisite:** Phase F at compare or authoritative — the sizer needs
cost-corrected expected EV, which Phase F provides.

**Exit criterion:** Phase H authoritative for paper; sized notional
matches sizer output for 14 days; per-ticker / correlation-bucket / portfolio
caps observably bind.

### Phase 4 — CPCV gate flip with Option C (forced-evaluation, 6–10 weeks)

The TECH_DEBT register's recommended path: a one-time forced-evaluation
pass with relaxed thresholds (`min_trades=5`, accept provisional ratings
on `n_paths<20`), then flip `CHILI_CPCV_PROMOTION_GATE_ENABLED=true`.

**Prerequisite:** at least Phase 1 Flag 1 has run for 30+ days so the
survival classifier has predictions for promoted patterns; that's the
defensive backstop in case CPCV under-evaluates.

**Steps:**

1. Build the forced-evaluation script (separate from the gate flip).
2. Run forced-evaluation in shadow; export results.
3. Operator review of which patterns demote / pass under relaxed thresholds.
4. Flip the gate. Monitor for promotion-pipeline narrowing.

**Exit criterion:** Promotion bar is consistent across all patterns;
no two-tier population.

### Phase 5 — Canonical truth-layer schema (12+ weeks)

The audit's most strategic recommendation. New tables `trading_feature_rows`
and `trading_label_rows` keyed by `(ticker, asset_class, bar_interval,
bar_start_at, regime, sector, benchmark_context, event_flags, ...)`.

This is not "another scanner" — it's an integration project: existing
data in `trading_snapshots`, `trading_pattern_trades`,
`trading_triple_barrier_labels`, and `trading_execution_events` all flows
into one canonical row keyed object. The same row supports training,
validation, promotion, and post-trade attribution.

**This deserves a dedicated design ADR before implementation** (per
project's `engineering:architecture` skill). Estimated 6+ weeks of work
once the design is approved.

## Phases NOT in this plan

* **Adding more strategy families.** The audit is right — concentration
  on momentum/breakout is real, but the higher-value work is depth (more
  evidence per pattern), not breadth (more patterns to validate at the
  weak current standard).
* **More indicators.** Feature schema v2 (regime, sector, SPY, earnings
  flags) WILL be added as part of Phase 5 work. Standalone v2 work
  without the truth-layer integration would create yet more fragmented
  storage.

## Open questions for the operator

1. **Risk appetite for Phase 2.** Compare-mode is observation-only.
   Authoritative-for-paper changes paper-trade gating. Operator
   comfortable proceeding when divergence reports look stable, or want
   30 days of dwell time first?
2. **Phase 4 CPCV flip timing.** The TECH_DEBT register recommends
   Option C (forced-evaluation pass with relaxed thresholds). Operator
   approves Option C, or prefers Option A (auto-demote, strict) or
   Option B (grandfather, permissive)?
3. **Phase 5 design ADR scope.** Schema-only ADR, or full integration
   plan including the new internal APIs the third-party audit suggested
   (`/features/backfill`, `/feature-health`, `/validation/recertify`,
   `/strategy-runtime/{pattern_id}`, `/execution/reconcile-order`)?
4. **Resource allocation.** Audit suggests "one backend engineer,
   one quant/ML engineer, part-time ops/frontend" for the first 4–6
   weeks. Solo-developer reality means staging will be slower; is
   that acceptable, or is the priority to ship Phase 2 fast even if
   Phase 3+ slips?
