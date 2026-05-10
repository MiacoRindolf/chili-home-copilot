# NEXT_TASK: f-promotion-pipeline-rebalance

STATUS: PHASE_2_DONE (Phases 3-6 ship in subsequent CC sessions per brief sequencing)

## Goal

**Algo-trader-architect overhaul of the brain's pattern promotion
pipeline.** The brain mines plenty of high-quality patterns (769 active,
586 mineable) but only **3 are promoted** — pattern 1011, 1016, and 585
(re-promoted manually 2026-05-09 after a noise-driven auto-demote).

The promotion-and-demotion pipeline is mis-calibrated: pattern 585 had
CPCV median sharpe 1.40, deflated sharpe 1.0, PBO 0.0, gate passed —
and got auto-demoted on n=8 realized trades because the autotrader's
7-stage gate chain filters out 99% of imminent alerts. The 8 trades
that survived weren't a random sample — they were gate-laundered noise.

The full brief is at
`docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md`
— **read it first.** Multi-phase initiative; CC ships one phase per
session. ~6 phases × 2-4h CC each = 12-24h CC total.

## Why now

Without this rebalance:
- Pattern 585 will be auto-demoted again at next 02:15 PT audit run
- Roster decays toward zero (demotes outpace promotions)
- Autotrader has insufficient fuel; trades won't happen

With this rebalance:
- Roster grows ~5-10/week, capped — gets to 30+ promoted in a quarter
- Promotion uses directional-correctness (gate-noise-free) as eval signal
- shadow_promoted lifecycle decouples observation from execution
- Risk-asymmetric: new patterns observe-only until they earn live

## Six phases (CC ships in order, one per session)

### Phase 1 — sample-size floor + AND-logic auto-demote (URGENT)
- Add `chili_pattern_demote_min_realized_trades=30` and
  `chili_pattern_demote_require_cpcv_degrade=True`
- Modify auto-demote audit: don't demote on realized stats with n<30;
  don't demote on realized when CPCV is still passing
- Tests in `tests/test_pattern_demote_thresholds.py`
- **Without Phase 1, pattern 585 dies again at 02:15 PT**

### Phase 2 — directional-correctness signal
- New table `pattern_alert_directional_outcome` (alert_id PK,
  directional_correct BOOLEAN, etc.)
- New scheduler job `pattern_directional_outcome_evaluator` (every 30
  min) that fetches OHLC post-window and computes directional accuracy
- Aggregate view per-pattern rolling-30 directional WR
- Tests in `tests/test_pattern_directional_outcome.py`
- This is the **clean eval signal** Phases 3-4 need

### Phase 3 — shadow_promoted lifecycle stage
- Add `shadow_promoted` to valid `lifecycle_stage` values
- `scan_pattern_eligible_main_imminent` returns True for shadow_promoted
  → patterns fire alerts
- `auto_trader.py` routes shadow_promoted-pattern alerts to **shadow-
  log only** regardless of LIVE flag → no broker call, no Trade row
- New flag `chili_shadow_promoted_lifecycle_enabled` (default True)
- RH path BYTE-IDENTICAL for non-shadow-promoted patterns (parity test)
- Tests in `tests/test_shadow_promoted_lifecycle.py`

### Phase 4 — composite quality scoring + weekly cohort auto-promote
- New column `quality_composite_score` on `scan_patterns`
- Nightly job computes:
  `composite = w1*cpcv_sharpe + w2*deflated_sharpe + w3*(1-pbo) +
   w4*directional_wr + w5*(1-decay)`
  with operator-tunable weights (5 settings)
- Weekly job promotes top-N candidates (N=20) to `shadow_promoted`
  with cap (max_per_week=10)
- Eligibility: `lifecycle IN ('backtested','candidate') AND
  promotion_gate_passed AND cpcv_median_sharpe >= 1.0`
- Default `chili_cohort_promote_enabled=False` until operator opts in
- Tests in `tests/test_pattern_cohort_promote.py`

### Phase 5 — per-pattern universe (use scope_tickers)
- `pattern_imminent_alerts.py` uses `pattern.scope_tickers ∩ tradable_universe`
  when scope_tickers is non-null
- Falls back to global universe (current behavior) when null
- New skip reason `pattern_scope_tickers_unavailable`
- Tests in `tests/test_pattern_per_pattern_universe.py`

### Phase 6 — verification + docs
- 7-day soak on new pipeline; pattern roster screenshots before/after
- CC report at canonical path
- Update `docs/STRATEGY/CURRENT_PLAN.md` with new architecture as
  canonical reference

## Acceptance criteria summary (across all phases)

After all 6 phases ship + 7-day verification:

1. ≥15 patterns at `lifecycle_stage IN ('promoted','live','shadow_promoted')`
   total (currently 3)
2. Pattern 585 still alive (Phase 1 sample floor protected it)
3. `pattern_alert_directional_outcome` populated; per-pattern directional
   WR computable
4. Cohort promote ran ≥1 weekly cycle; advanced ≥5 patterns to
   shadow_promoted
5. RH equity autotrader path BYTE-IDENTICAL pre/post
6. No new connection leaks (FIX 46 hygiene preserved)
7. All new feature flags default to safe values
8. CC reports for each phase + final verification

## Hard constraints (binding)

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **"Don't mess up the current working system but just enhance it"** —
  every change additive, opt-in via flag, with rollback.
- **No autotrader entry-side gate weakening.** Phase 3's shadow-log
  path uses existing shadow-log code; no new bypass.
- **No removal of existing demote logic.** Phase 1 ADDS conditions;
  old conditions still apply when sample size is large enough and CPCV
  agrees.
- **Edit-tool truncation discipline (HARD).** Multiple large files
  touched: `auto_trader.py` (~1700 lines), `pattern_imminent_alerts.py`,
  `opportunity_scoring.py`, `app/migrations.py`. After every edit:
  `wc -l` + `git diff --stat` + AST parse.

## Out of scope

- Autotrader gate chain changes (rule floor / LLM / PDT / cost-gate)
- Bracket writer / exit monitor changes
- Broker adapter changes
- New pattern types or mining algorithm changes
- Backtest engine changes

## Sequencing for CC running unattended

CC runs **one phase per session**:
1. Read `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md`
2. Read CLAUDE.md + STRATEGY/PROTOCOL.md
3. Pick the highest-priority unfinished phase
4. Truncation scan
5. Implement
6. Tests
7. Run pytest
8. Force-recreate workers
9. Verify
10. Write per-phase CC report at
    `docs/STRATEGY/CC_REPORTS/<date>_f-promotion-pipeline-rebalance-phase<N>.md`
11. Commit + push

After Phase 6 (verification), write the final summary report and mark
this NEXT_TASK as DONE.

## Rollback plan (per-phase)

- Phase 1: `chili_pattern_demote_require_cpcv_degrade=False` reverts
- Phase 2: drop `pattern_alert_directional_outcome` table + remove job
- Phase 3: `chili_shadow_promoted_lifecycle_enabled=False` reverts
- Phase 4: `chili_cohort_promote_enabled=False` reverts
- Phase 5: `git revert` the scope_tickers branch
- Phase 6: doc-only

## What CC should do if unsure

1. Phase 1 fails to protect pattern 585 → STOP, the demote logic may
   live in a different module than expected.
2. Phase 2 directional-evaluator can't fetch OHLC → log+skip; robust to
   gaps.
3. Phase 3 introduces regression in autotrader byte-identical parity
   → STOP. Parity test is HARD GATE.
4. Phase 4 cohort selects too many or too few → tune the cap; surface
   in per-phase report.
5. Phase 5 `scope_tickers` schema mismatch → adapt parser to actual
   schema (JSON list vs CSV).
6. Multi-process settings divergence after force-recreate → verify all
   4 worker containers see new settings before declaring done.

## Tonight's prep (already done by Cowork)

- Pattern 585 manually re-promoted (UPDATE scan_patterns SET
  lifecycle_stage='promoted', demoted_at=NULL WHERE id=585)
- Eligible patterns now: 3 (was 2)
- Alert flow should resume on next pattern_imminent_scanner run
- Phase 1 needs to ship before 02:15 PT or 585 gets re-demoted

## Memory notes for CC

- The autotrader's 7-stage gate chain (kill switch / drawdown breaker /
  rule floor / LLM / cost-gate / cap-check / bracket writer) protects
  capital. Promotion mistakes don't lose money directly. We can be
  less conservative on promotion eval.
- The promoted-pattern roster bug class: noise demotes faster than
  signal promotes. Phases 1+2+4 collectively fix this.
- Phase 3's shadow_promoted is conceptually identical to the Phase-6
  Coinbase paper-soak pattern, applied at the pattern-eval level
  instead of the venue level.
