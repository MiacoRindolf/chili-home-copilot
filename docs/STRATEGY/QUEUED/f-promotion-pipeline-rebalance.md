# f-promotion-pipeline-rebalance

**Owner**: Cowork → Claude Code (multi-phase initiative)
**Status**: PENDING
**Risk**: LOW for Phases 1-2 (additive); MEDIUM for Phase 3 (new lifecycle
stage); LOW for Phase 4-5 (additive scoring + universe routing); LOW for
Phase 6 (verification only).
**Time budget**: ~6 phases × 2-4h CC each = 12-24h CC total. Operator
can leave CC running across multiple sessions.

## Goal — algo-trader-architect framing

The brain mines lots of patterns (769 currently in `scan_patterns`, 586
active). The autotrader needs a steady supply of **promoted** patterns to
fire `pattern_imminent` alerts on. As of 2026-05-09:

- Currently promoted: **3** (pattern 1011, 1016, 585 — last re-promoted
  manually after auto-demote disaster)
- In OOS-pending limbo: 17
- Demoted (any reason): 35+
- Effective promotion rate: ~0.4%

**The pipeline is too slow because the bar is wrong, not because the bar
is high.** Pattern 585 had CPCV median sharpe 1.40, deflated sharpe 1.0,
PBO 0.0, gate passed — and got auto-demoted on n=8 realized trades because
the autotrader's downstream gate stack (12% rule floor, LLM, PDT, cost-
gate, etc.) filtered out 99% of its 1,284 alerts. The 8 trades that
survived weren't a random sample of the pattern's predictions; they were
gate-laundered noise.

This brief rebalances the pipeline to ship more high-quality patterns
**without** taking on capital risk during evaluation, **without** breaking
the existing autotrader, and **without** weakening any of the hard
safety belts.

## Operator-stated constraints (binding)

1. **"Don't mess up the current working system but just enhance it."**
   Every change must be additive, opt-in via flag, with rollback.
2. **"Be thorough as an algo trader architect."** Each phase has explicit
   acceptance criteria, parity tests, and a verification step.
3. **All Hard Rules from CLAUDE.md remain.** Especially Rule 1 (live-
   placement safety belts) and Rule 5 (prediction-mirror authority).

## The four real architectural problems

### Problem 1: promotion gate and trade gate are conflated

`lifecycle in ('promoted', 'live')` currently means TWO things:
- Eligible to fire imminent alerts (observability — costs nothing)
- Eligible to translate alerts into real trades (execution — costs capital)

These should be separate ladders. A pattern should fire alerts at lower
bars (so we can observe its directional accuracy) and only drive money
at higher bars (so we don't lose).

### Problem 2: realized P&L is contaminated by autotrader-gate noise

Pattern 585 fired 1,284 alerts; only 8 became actual trades after the
autotrader's 7-stage gate chain filtered. Those 8 weren't a random sample
of the pattern's directional calls — they were the calls that happened
to pass `projected_profit ≥ 12%`, `not LLM-blocked`, `not PDT-cooldown`,
etc. The 8-trade realized WR is gate-laundered noise, not pattern signal.

The clean signal is **directional correctness**: did price move ≥X% in
the predicted direction within Y hours of the alert? Measured on **all**
imminent alerts, not just gate-survivors.

### Problem 3: auto-demote uses single-condition OR logic

`f-pattern-demote-on-thin-evidence` (the audit running daily at 02:15 PT)
demotes when ANY ONE of {CPCV degrades, realized WR drops, sample-size
violation, evidence gap} fires. With small-n realized data, noise demotes.

Should require: **CPCV degrades AND realized degrades** — both must
indicate trouble before we touch a pattern that originally passed gates.

### Problem 4: no cohort-promotion ramp; no per-pattern universe

- Promotion appears manual or via specific migrations (mig 197 promoted
  via backtest evidence, one-time backfill). Without ongoing ramp, the
  roster decays: demotes outpace new promotions.
- Patterns 1011/1016 have `scope_tickers=NULL` and `ticker_scope='universal'`
  → pattern_imminent_scanner uses global 160-ticker universe → off-hours
  / weekends, that universe doesn't include their backtest tickers →
  `pattern_no_tickers` skip every cycle.

## Six-phase implementation

### Phase 1 — AND-logic auto-demote + sample-size floor (LOW risk; ship first)

**Why first**: Tomorrow at 02:15 PT the auto-demote audit will re-fire on
pattern 585 (now re-promoted manually) with n=8 trades — same data that
demoted it before. Without this fix, the operator will wake up to alert
flow being dead again.

**The change** (in whichever module owns the auto-demote sweep — most
likely `app/services/trading/pattern_evidence_audit.py` or the `f-pattern-
demote-on-thin-evidence` shipped path):

1. Add settings:
   - `chili_pattern_demote_min_realized_trades` (default `30`) — don't
     demote on realized stats with fewer than this many trades
   - `chili_pattern_demote_require_cpcv_degrade` (default `True`) — when
     True, demoting on realized stats also requires CPCV evidence to
     have degraded
2. Modify the auto-demote logic:
   - If `trade_count < chili_pattern_demote_min_realized_trades` →
     **never demote on realized stats**. Log "insufficient_sample" and
     continue. Pattern survives until trades accumulate.
   - If demoting on realized stats AND `chili_pattern_demote_require_cpcv_
     degrade=True` AND CPCV is still passing → **don't demote**. Log
     "realized_thin_but_cpcv_strong" and continue.
3. Add tests in `tests/test_pattern_demote_thresholds.py` (≥6 cases):
   - n=8, low realized WR, strong CPCV → no demote (this is pattern 585)
   - n=8, low realized WR, weak CPCV → no demote (still under sample floor)
   - n=50, low realized WR, strong CPCV → no demote (CPCV protects)
   - n=50, low realized WR, weak CPCV → demote (both signals agree)
   - n=50, OK realized, strong CPCV → no demote
   - Edge: n=29, low realized → no demote (just under floor)

**Acceptance criteria**:
- Pattern 585 survives the next 02:15 PT audit run (verifiable next day)
- No false demotes of patterns with `cpcv_median_sharpe ≥ 1.0 AND
  trade_count < 30`
- Test parity: existing demote tests still pass

**Rollback**: `chili_pattern_demote_require_cpcv_degrade=False` reverts
to old behavior.

---

### Phase 2 — Directional-correctness signal (gate-noise-free pattern eval)

**Why**: Realized P&L is gate-laundered. The clean signal is "did the
pattern's directional prediction come true within the hold window."

**The change**:

1. New table via migration `_migration_NNN_pattern_alert_directional_outcome()`:
   ```sql
   CREATE TABLE IF NOT EXISTS pattern_alert_directional_outcome (
     id BIGSERIAL PRIMARY KEY,
     alert_id BIGINT NOT NULL REFERENCES trading_alerts(id),
     scan_pattern_id BIGINT NOT NULL,
     ticker VARCHAR(32) NOT NULL,
     alert_at TIMESTAMP WITH TIME ZONE NOT NULL,
     predicted_direction VARCHAR(8) NOT NULL,    -- 'up' or 'down'
     entry_price NUMERIC,
     hold_window_hours INTEGER NOT NULL,         -- typically 4-24
     -- post-window outcome:
     window_close_at TIMESTAMP WITH TIME ZONE,
     window_max_favorable_pct NUMERIC,           -- best move in predicted direction
     window_max_adverse_pct NUMERIC,
     directional_threshold_pct NUMERIC NOT NULL, -- e.g., 1.5%
     directional_correct BOOLEAN,                -- max_favorable >= threshold?
     UNIQUE (alert_id)
   );
   CREATE INDEX idx_padc_pattern ON pattern_alert_directional_outcome(scan_pattern_id);
   CREATE INDEX idx_padc_alert_at ON pattern_alert_directional_outcome(alert_at);
   ```
2. New scheduler job `pattern_directional_outcome_evaluator` (every 30
   min, runs after the hold window closes):
   - For each `trading_alerts` row of type `pattern_breakout_imminent`
     where `alert_at + hold_window_hours <= now()` and no row in
     `pattern_alert_directional_outcome` yet:
     - Fetch OHLC for `ticker` from `alert_at` to `alert_at +
       hold_window_hours` (use existing `fetch_ohlcv_df`)
     - Compute `window_max_favorable_pct` and `window_max_adverse_pct`
     - Set `directional_correct = (max_favorable >= directional_threshold)`
     - Insert row
3. New view `pattern_directional_quality_v` aggregating per-pattern:
   - rolling 30-alert directional WR
   - rolling 30-alert sample size
   - last_evaluated_at
4. Tests in `tests/test_pattern_directional_outcome.py` (≥4 cases):
   - alert with positive favorable move ≥ threshold → correct=True
   - alert with weak favorable move < threshold → correct=False
   - alert with adverse move (price went opposite direction) → correct=False
   - alert too recent (window not yet closed) → no row inserted (skipped)

**Acceptance criteria**:
- After 24h of running, `pattern_alert_directional_outcome` has rows for
  all closed-window alerts from the last 24h
- Aggregate view shows per-pattern directional WR, distinct from
  trade-based realized WR
- No regression on existing pattern stats

**Rollback**: drop the new table + remove the scheduler job.

---

### Phase 3 — Two-stage lifecycle: `shadow_promoted` (decouple promotion gate from trade gate)

**Why**: We want promising patterns to fire alerts (cheap — observability)
while NOT yet driving money (expensive — capital risk). Right now both
are gated by `lifecycle in ('promoted','live')` together.

**The change**:

1. Add `shadow_promoted` to the valid set of `lifecycle_stage` values
   via migration `_migration_NNN_lifecycle_shadow_promoted()`.
2. Modify `scan_pattern_eligible_main_imminent` in
   `app/services/trading/opportunity_scoring.py`:
   ```python
   def scan_pattern_eligible_main_imminent(pat: ScanPattern) -> bool:
       life = (getattr(pat, "lifecycle_stage", None) or "").strip().lower()
       promo = (getattr(pat, "promotion_status", None) or "").strip().lower()
       # NEW: shadow_promoted patterns also fire imminent alerts
       if life in ("promoted", "live", "shadow_promoted"):
           return True
       if promo == "promoted":
           return True
       return False
   ```
3. Modify `auto_trader.py` entry routing: when the alert's source pattern
   has `lifecycle_stage='shadow_promoted'`, **route to shadow-log only**
   (regardless of `CHILI_COINBASE_AUTOTRADER_LIVE` value). The autotrader
   already has the shadow-log code from Phase 3 of f-coinbase-autotrader-
   enablement; this re-uses it for a pattern-eval purpose.
4. New helper `is_shadow_promoted_pattern(scan_pattern_id, db)` that the
   autotrader checks before any broker call.
5. Audit row writes new reason `selector:shadow_promoted_pattern_eval`
   distinct from `selector:coinbase_routing_shadow_log`.
6. Tests in `tests/test_shadow_promoted_lifecycle.py` (≥6 cases):
   - Pattern with `lifecycle='shadow_promoted'` → eligible_main_imminent
     returns True (alerts fire)
   - Alert from shadow_promoted pattern → autotrader routes shadow-log,
     does NOT call broker
   - Pattern with `lifecycle='promoted'` → autotrader behaves
     byte-identically to pre-Phase-3 (parity test)
   - Pattern with `lifecycle='challenged'` → not eligible (no alerts)
   - Mixed: one alert from shadow_promoted + one from promoted in same
     tick → first goes shadow, second goes through normal path

**Acceptance criteria**:
- shadow_promoted patterns fire alerts but do NOT result in any broker
  call or Trade row
- promoted/live patterns continue routing exactly as before (byte-
  identical via parity test)
- Audit log distinguishes shadow_promoted_pattern_eval from existing
  shadow-log paths

**Rollback**: Set a feature flag
`chili_shadow_promoted_lifecycle_enabled` (default True after Phase 3
ships). Flipping to False makes shadow_promoted patterns ineligible for
imminent alerts — pre-Phase-3 behavior.

---

### Phase 4 — Composite quality scoring + weekly cohort auto-promote to shadow_promoted

**Why**: Promotion needs an automated ramp. Without one, the roster
decays toward zero. With Phase 3 in place, ramping into shadow_promoted
is risk-free (no capital exposure).

**The change**:

1. New view / function `pattern_quality_composite_score(pattern_id)`:
   ```
   composite = w1 * normalize(cpcv_median_sharpe, target=2.0)        # offline
             + w2 * normalize(deflated_sharpe, target=1.0)            # multi-comparison
             + w3 * (1 - clip(pbo, 0, 1))                              # robustness
             + w4 * directional_correctness_wr_30                      # observed accuracy
             + w5 * (1 - decay_rate_30d)                               # has edge decayed?
             + bonus if promotion_gate_passed
   ```
   Default weights via settings (operator-tunable):
   - `chili_quality_weight_cpcv_sharpe` = 0.30
   - `chili_quality_weight_deflated_sharpe` = 0.20
   - `chili_quality_weight_pbo` = 0.10
   - `chili_quality_weight_directional_wr` = 0.25
   - `chili_quality_weight_decay` = 0.15
2. Nightly job `pattern_quality_score_refresh` (cron 23:30 PT, after
   most evaluations have run):
   - For each active `scan_patterns` row, compute composite + write to
     a new column `quality_composite_score` (decimal 0..1)
3. Weekly job `pattern_cohort_promote` (cron Sun 22:00 PT):
   - Eligibility set: active patterns with
     `lifecycle_stage IN ('backtested','candidate')` AND
     `promotion_gate_passed=True` AND `cpcv_median_sharpe >= 1.0` AND
     NOT in `shadow_promoted/promoted/live`
   - Sort by composite score DESC
   - Take top N (default 20, via setting `chili_cohort_promote_top_n`)
   - Set `lifecycle_stage='shadow_promoted'`, log audit
4. Cap: `chili_cohort_promote_max_per_week` (default 10) — never advance
   more than this many net new promoted/live per week (counts shadow→
   live promotions too)
5. Tests in `tests/test_pattern_cohort_promote.py` (≥5 cases):
   - 20 candidates + score → top 20 promoted (or capped to max-per-week)
   - Already-promoted pattern → not advanced again
   - Pattern with `promotion_gate_passed=False` → not eligible
   - Pattern with `cpcv_median_sharpe<1.0` → not eligible
   - Cap respected (e.g., 30 candidates + cap=10 → only 10 advance)

**Acceptance criteria**:
- After one weekly run, ≥10 new shadow_promoted patterns exist
- No pattern can be promoted twice in same week
- All cohort promotions go to `shadow_promoted` (not directly to `promoted`)
- Operator can disable via `chili_cohort_promote_enabled=false`

**Rollback**: Flag-disable; existing manually-promoted patterns
unaffected.

---

### Phase 5 — Per-pattern universe (use scope_tickers when available)

**Why**: Currently `pattern_imminent_alerts.py` uses global 160-ticker
universe. Patterns 1011/1016 have `scope_tickers=NULL` so they fall back
to global; off-hours/weekends global universe shrinks and they hit
`pattern_no_tickers` skip every cycle.

**The change**:

1. In `pattern_imminent_alerts.py`, modify the per-pattern eval loop:
   - If `pattern.scope_tickers` is non-null (list/JSON), use it
     intersected with currently-tradable tickers
   - Otherwise fall back to global universe (current behavior)
2. New skip reason `pattern_scope_tickers_unavailable` when intersection
   is empty (distinct from `pattern_no_tickers`)
3. Tests in `tests/test_pattern_per_pattern_universe.py` (≥4 cases):
   - Pattern with scope_tickers=['AAPL','MSFT'] + universe contains both
     → both evaluated
   - Pattern with scope_tickers=['AAPL'] + universe doesn't contain AAPL
     → skipped with new reason
   - Pattern with scope_tickers=NULL → uses global (parity with pre-Phase-5)
   - Pattern with empty scope_tickers list → uses global (forgiving)

**Acceptance criteria**:
- Patterns with scope_tickers set evaluate against their own universe
- Patterns without scope_tickers behave byte-identically to pre-Phase-5
- Pattern 1011/1016 either get explicit scope_tickers populated (manual
  or via backfill query) OR continue using global universe — operator
  decides

**Rollback**: revert the scope_tickers branch in pattern_imminent_alerts.

---

### Phase 6 — Verification + docs

1. Run a 7-day shadow on the new pipeline. Compare:
   - Number of patterns at each lifecycle stage
   - Cumulative shadow_promoted patterns advancing to promoted via
     directional-correctness gate
   - Roster size over time (should grow by ~5-10/week, capped)
   - No regression on autotrader behavior (real-money trades same or
     better)
2. Write CC report
   `docs/STRATEGY/CC_REPORTS/<date>_f-promotion-pipeline-rebalance.md`
   with: per-phase verdict, before/after pattern roster screenshots,
   recommendation for next adjustment.
3. Update `docs/STRATEGY/CURRENT_PLAN.md` with the new pipeline
   architecture as the canonical reference.

## Sequencing for CC

CC should ship **one phase per session**, in order. Each phase has its
own: brief read, code edit, tests written, pytest green, force-recreate,
verify, commit. After each phase, surface for operator review.

If running unattended, CC should commit at the end of each phase + write
a per-phase mini-report at
`docs/STRATEGY/CC_REPORTS/<date>_f-promotion-pipeline-rebalance-phase<N>.md`.

**Phase order**:

1. **Phase 1** (sample-size floor + AND-logic demote) — IMMEDIATE need.
   Without this, pattern 585 gets re-demoted at next 02:15 PT audit and
   alert flow dies again.
2. **Phase 2** (directional-correctness signal) — provides the clean
   eval signal Phases 3-4 need.
3. **Phase 3** (shadow_promoted lifecycle) — decouples observation from
   execution.
4. **Phase 4** (composite scoring + cohort promote) — drives the
   automated ramp.
5. **Phase 5** (per-pattern universe) — fixes the `pattern_no_tickers`
   skip on patterns 1011/1016.
6. **Phase 6** (verification) — measure + document.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Kill switch /
  drawdown breaker / ensemble promotion check / rule floor / LLM /
  cost-gate / cap-check / bracket writer all unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched. The directional-
  correctness signal is a NEW table, not a write to the prediction mirror.
- **No autotrader entry-side gate weakening.** Phase 3's shadow-log path
  uses the existing shadow-log code; no new bypass introduced.
- **No removal of existing demote logic.** Phase 1 ADDS conditions to
  the demote check; old conditions still apply when sample size is
  large enough and CPCV agrees.
- **All new behavior gated by feature flags.** Each phase has a flag
  (default ON for Phases 1-2, off for Phase 4 cohort-promote until
  operator opts in).

## Out of scope

- Changes to autotrader gate chain (rule floor, LLM, PDT, cost-gate, etc.)
- Changes to bracket writer / exit monitor
- Changes to broker adapters (RH or Coinbase)
- New pattern types or mining algorithm changes
- Changes to backtest engine

## What CC should do if unsure

1. **Phase 1 fails to make pattern 585 survive overnight**: STOP.
   Surface for operator. May indicate the demote logic lives in a place
   I didn't anticipate; need operator to confirm where the audit runs.
2. **Phase 2 directional-evaluator can't fetch OHLC for crypto tickers
   off-hours**: log + skip; not a blocker. Some tickers may have
   intermittent data; the system should be robust to gaps.
3. **Phase 3 introduces a regression in autotrader byte-identical
   parity**: STOP. RH path is byte-identical or nothing ships.
4. **Phase 4 cohort-promote selects too many or too few**: tune the cap
   `chili_cohort_promote_max_per_week`; surface in the per-phase report.
5. **Phase 5 `scope_tickers` schema mismatch**: read the actual column
   shape (`scope_tickers` JSON list vs comma-separated string). Adapt
   the parser to whatever the existing schema uses.
6. **Multi-process settings divergence after a force-recreate**: same
   class as the autotrader-flag bug from earlier this week. Verify all
   4 worker containers see new settings before declaring a phase done.

## Memory recommended for next CC sessions

- New memory: pattern 585 manual re-promote 2026-05-09 19:47 UTC; will
  be auto-demoted at 02:15 PT next day unless Phase 1 ships first
- New memory: composite quality scoring weights + thresholds (operator
  may tune; document the rationale)

## Recovery / cleanup notes

- This brief WILL touch `app/services/trading/opportunity_scoring.py`,
  `app/services/trading/pattern_imminent_alerts.py`,
  `app/services/trading/auto_trader.py`,
  `app/migrations.py`, `app/config.py`,
  `app/services/trading_scheduler.py`. All large files; truncation scan
  before AND after every edit.
- Each phase's tests in its own file: `tests/test_pattern_demote_thresholds.py`,
  `tests/test_pattern_directional_outcome.py`,
  `tests/test_shadow_promoted_lifecycle.py`,
  `tests/test_pattern_cohort_promote.py`,
  `tests/test_pattern_per_pattern_universe.py`.

## Acceptance criteria summary (across all phases)

After all 6 phases ship and 7-day verification soak:

1. ≥15 patterns at `lifecycle_stage IN ('promoted','live','shadow_promoted')`
   total (currently 3)
2. Pattern 585 still alive (not auto-demoted) due to Phase 1 sample
   floor
3. `pattern_alert_directional_outcome` table populated; per-pattern
   directional WR computable
4. Cohort promote ran ≥1 weekly cycle; advanced ≥5 patterns to
   shadow_promoted
5. RH equity autotrader path BYTE-IDENTICAL pre/post
6. No new connection leaks (FIX 46 hygiene preserved)
7. All new feature flags default to safe values
8. CC reports for each phase + final verification report

## Why this is the right architecture

**Risk-asymmetric**: shadow_promoted patterns observe but don't trade.
Their CPCV is good (ensemble check before promotion). Their directional
correctness will get measured cleanly (Phase 2). They graduate to live
based on directional WR ≥ 0.55, NOT on autotrader-realized P&L (gate noise).

**Pace-honest**: with composite scoring + weekly cohort + cap, the
roster grows ~5-10/week without surge concentration. Realistically goes
from 3 promoted to 30+ in a quarter.

**Fool-proof**: no change weakens any existing safety belt. All new
behavior is gated. Rollback is per-phase.

**Architect-grade**: the eval signal (directional correctness) is
unbiased by the autotrader's gate chain — the question we ASK ("is this
pattern's prediction right?") matches the question we ANSWER ("here is
the directional WR"). Currently we ask one question and answer a
different one (gate-laundered realized P&L), which is why nothing ever
graduates.
