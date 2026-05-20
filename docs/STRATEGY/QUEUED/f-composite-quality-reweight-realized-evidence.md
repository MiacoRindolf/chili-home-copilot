# f-composite-quality-reweight-realized-evidence

> **Type:** Formula change + new realized-evidence inputs + cohort eligibility floor + one-shot demote-the-losers migration
> **Parent:** `f-promotion-pipeline-rebalance` (Phase 4 was the shipped composite; this is its corrective sibling)
> **Companion:** `f-composite-quality-event-driven` (refresh cadence) — orthogonal; can ship before or after.
> **Status:** unblocked. Replaces the composite weights actually used by `pattern_quality_score.compute_quality_composite_score`.

## Why this is the highest-leverage fix in the system right now

The composite score is **inversely correlated with realized PnL**. Measured 2026-05-16 by Cowork:

| n filter | Spearman rho (score → total PnL) | p-value |
|---|---:|---:|
| All scored patterns with realized trades (n=12) | **−0.757** | **0.0044** |
| With ≥5 realized trades (n=9) | −0.600 | 0.088 |

Top-half by composite score: total realized PnL **−$118.63**. Bottom-half: **+$597.80**. Pattern 585 (the only real edge, +$554 over 85 trades) sits at rank 10 of 12 by composite. Patterns 1066/1068/1073 (top 3 by composite, all backtest Sharpe 4+) are at **−$146 combined realized**.

Mechanism — the existing formula has two dead components:

```
composite = 0.30·clip(cpcv_sharpe/2.0)         ← discriminating but overfit OOS
          + 0.20·clip(deflated_sharpe/1.0)     ← pegged at 1.0 for 12/12 patterns → dead
          + 0.15·(1 - pbo)                     ← pegged at 0.0 for 12/12 patterns → dead
          + 0.25·directional_wr                ← real signal
          + 0.10·(1 - decay)                   ← real signal
```

DSR + PBO contribute 35% of the score as a **constant** (every pattern gets +0.35 from these). Of the remaining 65% that varies, 46% comes from CPCV Sharpe — which is in-sample-overfit on patterns with realized PnL that doesn't replicate. Result: the score systematically advances overfit backtests and demotes the OOS winners.

**If `CHILI_COHORT_PROMOTE_ENABLED=true` ever flips under the current formula, the weekly cohort job will systematically promote losers and dilute pattern 585's working edge.** That's the live landmine this brief disarms.

## Goal

Rewrite the composite formula so that

1. realized-PnL evidence is a first-class input (≥30% weight when n_realized_trades ≥ 5);
2. CPCV weight drops to ≤15% to limit the overfit-backtest bias;
3. dead components (DSR/PBO) drop to ≤5% each until they have demonstrated discriminatory power;
4. cohort-promotion eligibility gets a **realized-PnL floor**: cannot promote a pattern with `n_realized_trades ≥ 5 AND realized_avg_pnl_pct ≤ 0`;
5. one-shot demote-the-losers migration moves currently mis-promoted patterns back to `challenged` with an audit trail.

Composite score becomes a meaningful ranking signal — and re-enabling `CHILI_COHORT_PROMOTE_ENABLED` becomes a safe, monotonically-improving operation.

## Design

### New components

Two new inputs to `compute_quality_composite_score`:

- **`realized_pnl_score`** — normalized avg-realized-PnL-per-trade. Reads from `trading_trades` joined on `scan_pattern_id` for closed trades in the trailing window (default 90 days). Formula: `clip(avg_pnl_pct / w_norm, -1.0, 1.0) → map to [0, 1]` where `avg_pnl_pct = avg(pnl / (entry_price * quantity))` and `w_norm = 0.01` (1% per trade is full credit). Patterns with `n_realized_trades < 5` get `realized_pnl_score = None` (NULL propagation per advisor §2.6 — no magic default).
- **`realized_evidence_score`** — sample-size confidence multiplier. `1 - exp(-n_realized_trades / 30.0)`. At n=5 contributes ~15%, at n=30 contributes ~63%, at n=85 (pattern 585) saturates near 100%. Always defined when `n_realized_trades ≥ 1`.

These multiply: the effective realized contribution is `realized_pnl_score * realized_evidence_score`. A pattern with strong realized PnL but only 6 trades doesn't dominate; a pattern with 85 trades and meh PnL doesn't dominate either.

### New formula (default weights)

```
composite = 0.10·clip(cpcv_sharpe/2.0)            (was 0.30 — cut for overfit bias)
          + 0.05·clip(deflated_sharpe/1.0)        (was 0.20 — dormant until DSR varies)
          + 0.05·(1 - pbo)                        (was 0.15 — dormant until PBO varies)
          + 0.35·directional_wr                   (was 0.25 — clean OOS proxy)
          + 0.10·(1 - decay)                      (was 0.10 — unchanged)
          + 0.35·realized_pnl_score·realized_evidence_score   (NEW — first-class)
```

Sum: 1.00. All weights settings-driven via `chili_cohort_score_weight_*` (already in place for the first five; two new settings added per below).

**Backwards-compat:** when `n_realized_trades < 5` (insufficient realized data), the realized component contributes 0 and the other weights re-normalize so the score still lands in [0,1]. The function returns `None` only when CPCV / directional_wr / decay are still NULL (same behaviour as today for thin-evidence patterns).

### New cohort-promote eligibility floor

In `app/services/trading/pattern_cohort_promote.py` (and the corresponding SQL in the weekly job), add to the eligibility predicate:

```sql
AND (
  realized_n_trades < 5
  OR realized_avg_pnl_pct > 0
)
```

A pattern cannot be auto-promoted while it has ≥5 closed trades and a negative average. Below 5 trades (sample too small to demote on), promotion remains gated by the existing CPCV floor. The floor is settings-driven: `chili_cohort_promote_min_realized_trades_for_floor` (default 5) and `chili_cohort_promote_max_realized_avg_pnl_pct_negative` (default 0.0 — strictly above zero).

### One-shot demote-the-losers (mig 244)

Idempotent migration that:

1. SELECTs every pattern where `lifecycle_stage IN ('promoted','shadow_promoted','pilot_promoted')` AND `quality_composite_score IS NOT NULL` AND has ≥5 realized trades with negative `avg_pnl_pct`.
2. For each, emits a structured log line: `[chili_mig_244] pid=<id> old_stage=<stage> new_stage=challenged n_trades=<n> avg_pnl_pct=<pct> total_pnl=<dollars>`. Operator grep gives the audit trail.
3. UPDATEs `scan_patterns.lifecycle_stage = 'challenged'` (NOT `retired` — the pattern miner can still learn from them; just no longer trade-eligible).

**NOTE — corrected 2026-05-16 by CC plan-gate consult:** the brief originally specified an INSERT into `pattern_family_trial_log` with `verdict='demoted_by_composite_reweight_2026_05_16'`. That table's schema (mig 242) is purpose-built for BH-FDR variant tracking — columns are `hypothesis_family / variant_pattern_id / variant_dsr / variant_pbo / variant_promoted / family_best_dsr_at_time / family_variants_tested_so_far` — there is no `verdict` column and no free-form evidence payload. Dropping the DB-side log; structured-log audit is sufficient for a one-shot.

This is the immediate capital-protection action. Expected to affect 4-6 patterns (1066, 1067, 1068, 1073, 1216 — possibly 706 depending on whether the −$3.96 margin clears the floor; floor is "strictly above zero" so 706 also demoted).

### New settings (app/config.py)

```python
# f-composite-quality-reweight defaults — Cowork 2026-05-16
chili_cohort_score_weight_cpcv_sharpe: float = 0.10        # was 0.30
chili_cohort_score_weight_deflated_sharpe: float = 0.05    # was 0.20
chili_cohort_score_weight_pbo_inverse: float = 0.05        # was 0.15
chili_cohort_score_weight_directional_wr: float = 0.35     # was 0.25
chili_cohort_score_weight_decay_inverse: float = 0.10      # unchanged
chili_cohort_score_weight_realized: float = 0.35           # NEW

# Realized-PnL normalization
chili_cohort_score_realized_pnl_normalizer_pct: float = 0.01   # 1% avg = full credit
chili_cohort_score_realized_evidence_tau: float = 30.0          # n→inf: 1.0; n=30: ~0.63
chili_cohort_score_realized_window_days: int = 90               # trailing-90 by default

# Cohort eligibility floor
chili_cohort_promote_min_realized_trades_for_floor: int = 5
chili_cohort_promote_max_realized_avg_pnl_pct_negative: float = 0.0  # strict > 0
```

Operator can override any of these via .env without code change — they're all read through `Settings`.

## Deliverables

D1. **`app/services/trading/pattern_quality_score.py`** — add realized-PnL component, new SQL helper `_load_realized_pnl_map(db)` joining `trading_trades` filtered to `scan_pattern_id IS NOT NULL AND scan_pattern_id != -1 AND exit_date > NOW() - INTERVAL '{window_days} days'`. Extend `compute_quality_composite_score` signature: `(pat, directional_wr, decay, weights, realized_pnl_score, realized_n_trades)`. NULL-propagation rules updated (see "Backwards-compat" above).

D2. **`app/config.py`** — eight new settings above (five default-changes + three new).

D3. **`app/services/trading/pattern_cohort_promote.py`** — eligibility WHERE-clause floor.

D4. **`app/migrations.py`** — `_migration_244_composite_reweight_demote_losers()`. Idempotent (won't re-demote a pattern that's already at `challenged`). Audit-logs to `pattern_family_trial_log` per row. Migration ID 244.

D5. **`tests/test_composite_reweight.py`** — covering:
   - Anti-corr regression: synth 10-pattern dataset where high CPCV maps to negative realized PnL, low CPCV maps to positive — assert the NEW formula produces Spearman rho > 0 (positive correlation) and the OLD formula produces rho < 0.
   - Realized-PnL component shape: at avg_pnl_pct = +1%, score = 1.0; at -1%, score = 0; at 0%, score = 0.5; below ≥5 trades, returns None.
   - Realized-evidence saturation: n=1 contributes ~3%, n=30 contributes ~63%, n=85 contributes ~94%.
   - Cohort eligibility floor: pattern with n=5 realized and avg_pnl_pct = -0.001 is REJECTED; pattern with n=4 realized and any avg_pnl_pct is allowed through; pattern with n=5 and avg_pnl_pct = +0.001 is allowed.
   - Mig 244 idempotency: run twice, second run is no-op; audit-log shows exactly one row per affected pattern.

D6. **Post-deploy verification (single command + report section in CC_REPORT):**
   - Re-run the anti-corr Spearman test on the now-recomputed scores. The brief is successful when Spearman(score, total_pnl) flips from −0.757 (today) to **≥ +0.30** (positive, statistically distinguishable from zero at n=12).
   - Capture top-15-by-new-score table for the CC_REPORT.
   - Capture mig 244 audit-log entries.

D7. **`docs/STRATEGY/CC_REPORTS/2026-05-{N}_f-composite-quality-reweight-realized-evidence.md`** — covering all of the above + the post-deploy Spearman re-measurement.

## Hard constraints

- **No magic-default fallbacks** (advisor §2.6). NULL propagation when realized data is missing.
- **All weights sum to 1.0** — assertion in `compute_and_persist_scores` (already exists as a warning; tighten to ValueError if sum diverges by >1%).
- **Migration 244 is idempotent and audit-logged.** No silent state changes. Operator can read `[chili_mig_244]` log lines and see exactly which pattern_ids moved and why.
- **Cohort-promote feature flag stays OFF.** This brief does NOT flip `CHILI_COHORT_PROMOTE_ENABLED`. It makes flipping it safe. Operator flips when ready, in a separate decision.
- **No autotrader / venue / broker touched.** Brain-side ranking change only.
- **Test DB uses `_test` suffix.** Per PROTOCOL.
- **Tests pass before deploy.** No exceptions.

## Why not "just lower CPCV weight"

A purely-subtractive reweight (cut CPCV, leave the rest) doesn't add the realized signal that's missing — it just rebalances among the existing three discriminating inputs (CPCV / directional_wr / decay). Realized PnL is the cleanest OOS signal we have; not using it leaves the formula blind to actual money outcomes. The whole point of the rebalance is to **anchor scoring on realized money**.

## Open questions for plan-gate consult

1. **Realized-PnL window: 90 days vs all-time?** Default 90 to keep the signal recent and let dead patterns age out. Alternative: all-time, on the theory that pattern 585 has 6+ months of history that's still informative. Recommendation: ship at 90 days, monitor Spearman post-deploy, lengthen to 180 if signal looks too noisy.
2. **Normalizer `w_norm = 0.01` (1% per-trade as full credit) — is this too generous?** **Corrected 2026-05-16:** pattern 585's actual avg_pnl_pct over 85 trades (90d window) is **1.631%**, not the 0.4% the brief originally estimated. At w_norm=0.01, 585's realized_pnl_score saturates at 1.0 (full credit). Losers around −0.5%/trade land at clip(−0.5)=−0.5 → mapped to 0.25. Mid-pack patterns around +0.2%/trade land at 0.60. The chosen normalizer cleanly discriminates 585 from the losers; CC verified by direct DB probe. Keep w_norm=0.01.
3. **Mig 244: demote-or-retire?** Demote to `challenged` keeps the pattern in the mining pool (the miner may discover that the pattern works in a different regime later). Retire removes it entirely. Recommendation: demote — the cost of keeping a challenged pattern is near zero, and we may learn something.

## Result

CC_REPORT writes the post-deploy Spearman number. The brief is **successful** if and only if:

- Spearman(new_score, total_pnl) ≥ +0.30 at n=12 (positive, opposite sign from today's −0.757).
- Mig 244 audit-logged at least 4 demotions (1066, 1068, 1073, 1216 — possibly 1067 and 706 if floor cuts them).
- Pattern 585 ends in the top-3 by new composite score.
- Tests all pass; no autotrader regression; no broker-routing change.

If any of those fail, CC reports back with the actual numbers and Cowork rewrites the normalizer / weights in a follow-up brief before shipping the cohort-promote flag flip.
