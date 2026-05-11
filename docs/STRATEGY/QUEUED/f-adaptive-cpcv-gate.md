# f-adaptive-cpcv-gate (Phase 2 of adaptive-promotion-architecture)

> **Type:** New module + feature flag + shadow log (app/ changes)
> **Parent:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Status:** unblocked. Phase 1b prod flag flipped 2026-05-11T17:19:45Z and verified
> functional (20 breakout_alert_resolved born pending, 1 market_snapshots_batch
> claimed, mine handler fired ev_id=4335). Phase 2 has no runtime dependency on
> Phase 1b — they touch different modules.

## Goal

Replace the hardcoded CPCV gate thresholds (`DSR ≥ 0.95`, `PBO ≤ 0.2`,
`median_sharpe ≥ 0.5`, `cpcv_n_paths ≥ 20`, `min_trades ≥ 30`) with
empirical, sample-size-aware ones that adapt to the active pattern
pool's distribution. Phase 0 found these hardcoded thresholds have
zero discriminatory power on the current population (DSR pegged at
1.000, PBO at 0.000 across all 39 patterns that have CPCV data).

## Design (carried forward from parent brief)

### Numbers that go away (arbitrary, no project-specific justification)
- `dsr >= 0.95` (promotion_gate.py:903) — Lopez de Prado convention, inherited
- `pbo <= 0.2` (line 909) — same
- `med_sh >= 0.5` (line 921) — same
- `cpcv_n_paths >= 20` (paths_provisional_min) — same
- `min_trades >= 30` (full_confidence_min_trades) — same

### Numbers that remain (operator policy, semantic meaning)
```python
chili_cpcv_target_promotion_pool_pct  = 0.05   # admit top 5% of active pool
chili_cpcv_ci_level                   = 0.90   # 90% CI on lower-bound
chili_portfolio_marginal_sharpe_min_bps = 0.0  # any positive marginal lift admits
```

### Mechanism

For each pattern with CPCV data:

1. **Bayesian shrinkage.** Each metric (DSR, PBO, med_sharpe) is shrunk
   toward the pool mean by sample-size-dependent weight
   `w = n / (n + n0)` where `n0` is the pool's median trade-count.
   Kills the "11-trade DSR=1.000" inflation pattern 585 exhibits.

2. **Sample-size-aware lower CI.** Hansen (2005) closed-form CI for
   deflated Sharpe at `chili_cpcv_ci_level` (default 90%). Bailey/Lopez
   bootstrap CI for PBO. Wide CI for low-n patterns; tight for high-n.

3. **Empirical percentile threshold.** Promotion eligible per metric if
   `lower_CI >= pool_percentile(q)` where `q` derives from
   `chili_cpcv_target_promotion_pool_pct` (e.g. 5% target → q=0.95 →
   admit top ~29 patterns by each metric).

4. **Pareto frontier (multi-objective).** Promotion eligible only if
   pattern is on the Pareto frontier of the pool across (shrunken DSR,
   shrunken PBO, shrunken med_sharpe). Prevents "checkbox-checked but
   not the best".

5. **Portfolio marginal Sharpe lift (optional, configurable).** Adding
   the pattern must improve portfolio CPCV median Sharpe by at least
   `chili_portfolio_marginal_sharpe_min_bps` (default 0.0 = any
   positive contribution admits; raise to tighten).

### Deliverables

1. **`app/services/trading/cpcv_adaptive_gate.py`** — new module wrapping
   `promotion_gate.promotion_gate_passes` with the adaptive logic.
   Behind feature flag `chili_cpcv_adaptive_gate_enabled` (default False).
   When flag is False, the wrapper is bypassed — byte-identical legacy
   behavior.

2. **`app/config.py`** — three new pydantic Settings fields:
   ```python
   chili_cpcv_adaptive_gate_enabled: bool = False
   chili_cpcv_target_promotion_pool_pct: float = 0.05
   chili_cpcv_ci_level: float = 0.90
   chili_portfolio_marginal_sharpe_min_bps: float = 0.0
   ```

3. **`app/migrations.py`** — migration 239 creates new table
   `cpcv_adaptive_eval_log` for shadow-log + post-hoc analysis:
   ```sql
   CREATE TABLE cpcv_adaptive_eval_log (
     id BIGSERIAL PRIMARY KEY,
     scan_pattern_id INT NOT NULL,
     evaluated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
     metric_name TEXT NOT NULL,
     raw_value FLOAT,
     shrunken_value FLOAT,
     lower_ci FLOAT,
     pool_percentile FLOAT,
     pool_threshold FLOAT,
     eligible BOOLEAN,
     pareto_dominant BOOLEAN,
     marginal_portfolio_sharpe_bps FLOAT,
     legacy_verdict_pass BOOLEAN,
     adaptive_verdict_pass BOOLEAN
   );
   CREATE INDEX ix_cpcv_adaptive_eval_log_pat
     ON cpcv_adaptive_eval_log (scan_pattern_id, evaluated_at DESC);
   ```

4. **`tests/test_cpcv_adaptive_gate.py`** — covers:
   - Flag-off parity: wrapper is no-op, legacy gate runs unchanged.
   - Shrinkage math: 11-trade pattern with raw DSR=1.0 shrinks toward
     pool mean; 300-trade pattern barely moves.
   - Empirical-percentile threshold: top-5% target → exactly the
     expected admission count given a synthetic pool distribution.
   - Pareto frontier: 3-metric synthetic dataset, dominated patterns
     are rejected even when each individual metric passes.
   - Portfolio marginal: pattern with negative correlation to existing
     roster passes more readily than positively-correlated.
   - Shadow-log write: each adaptive eval writes one row per metric +
     one summary row.

5. **Wiring point.** `promotion_gate.finalize_promotion_with_cpcv`
   (line 933) calls the wrapper after computing CPCV metrics. The
   wrapper computes both verdicts (legacy + adaptive), writes the
   shadow-log, and returns the legacy verdict unchanged unless
   `chili_cpcv_adaptive_gate_enabled` is True. Single call site, no
   other code changes.

6. **`docs/runbooks/CPCV_ADAPTIVE_GATE.md`** — operator runbook:
   - Reading the shadow log (sample SQL queries)
   - How to tune the three meta-parameters
   - How to flip the flag (env var + force-recreate)
   - Rollback procedure (flip flag, optionally truncate shadow log)

7. **`docs/STRATEGY/CC_REPORTS/2026-05-11_adaptive-cpcv-gate.md`** —
   standard CC_REPORT.

## Rollout sequence

1. **Ship at flag-OFF.** Module + table + tests land. Legacy gate
   continues exclusively.
2. **Enable shadow-log only** (no gate flip). Operator flips
   `chili_cpcv_adaptive_gate_enabled=true` BUT keeps the legacy gate
   as authority. The wrapper computes both verdicts, writes shadow-log,
   returns legacy verdict. Run for 7 days. Compare verdicts in shadow log.
3. **Flip authority to adaptive.** After shadow-log comparison shows
   the adaptive gate's verdicts are sound (no obviously-wrong
   promotions or rejections vs operator judgment), flip the wrapper to
   return the adaptive verdict.

Note: step 3 is operator-controlled. This brief only ships step 1.

## Hard constraints

- Flag defaults `False`. Merge produces zero behavior change. Reversible.
- No changes to `promotion_gate.promotion_gate_passes` itself —
  preserve as-is for byte-identical legacy path.
- The wrapper is the SINGLE call site for adaptive logic. Don't sprinkle
  the new computation across multiple files.
- The shadow-log table is the audit trail — write every evaluation,
  even when the flag is off (operators can opt into the log without
  flipping the gate).
- All numbers introduced are operator-policy parameters (target pool %,
  CI level, portfolio margin), not arbitrary thresholds. Document each
  in the runbook with the "operator policy not magic" framing.
- No autotrader / venue / broker touched.
- Migration is additive (new table + index). No column adds to
  scan_patterns.

## Open questions for operator (in consult)

1. **Target promotion pool size.** Default 5% → ~29 patterns of 586.
   Concern: too many live patterns dilutes attention. Defer or set
   tighter?
2. **CI level.** Default 90%. 95% is stricter (smaller pool), 80% is
   looser (larger pool).
3. **Portfolio marginal Sharpe gate.** Default 0.0 means "any positive
   contribution admits" — effectively a no-op. Operator may want it
   higher once shadow-log shows portfolio correlation patterns.

Brief defaults: 5% / 90% / 0.0. CC should surface these in consult.

## Success criteria

- All deliverables committed.
- CI green with flag False.
- Shadow-log table created (migration 239 passes).
- Tests cover flag-off parity + shrinkage + percentile + Pareto + portfolio.
- CC_REPORT documents the operator-chosen meta-parameter defaults.

## Why this is unblocked now (operator's question 2026-05-11)

Cowork's original brief gated Phase 2 on "Phase 1b prod flip clean for
24h". That gate was defensive, not architectural. Phase 2 lives in a
new module behind its own flag — it has no runtime dependency on
Phase 1b's behavior. The two can ship in parallel. The "soak" framing
applied to Phase 1c-large (backtest_completed/breakout_alert_resolved
backfill) where handler throughput under burst load is a real concern,
not to Phase 2's flag-off-default code module.
