# CC_REPORT: f-evaluation-function-fix-tier-a

**Session type:** Cowork-direct execution (operator approved all Tier A items in chat at 2026-05-18 17:30 PT after the architect/data-scientist audit).

## What shipped

**Single commit `23bde18`** on `main`, 7 files / 556 insertions / 5 deletions:

- `app/migrations.py` (+192) — migrations 245 and 246
- `app/models/trading.py` (+12) — five new ScanPattern columns
- `app/services/trading/learning.py` (+50) — payoff-ratio gate in `_matches_thin_evidence_criteria` AND `run_live_pattern_depromotion` (symmetric protection across both demote paths)
- `app/services/trading/pattern_quality_score.py` (+36) — composite-score realized-n floor in both `compute_and_persist_scores` and `compute_and_persist_scores_streaming`
- `app/services/trading/realized_stats_sync.py` (+70) — extended nightly sync to refresh `avg_winner_pct` / `avg_loser_pct` / `payoff_ratio` / `payoff_ratio_n` / `payoff_ratio_updated_at`
- `app/config.py` (+43) — 3 new settings: `chili_pattern_demote_payoff_ratio_floor=1.5`, `chili_pattern_demote_payoff_ratio_min_n=5`, `chili_composite_min_realized_trades=5`
- `tests/test_pattern_demote_payoff_ratio.py` (+158) — 9 new pinned tests

**Migrations added: 2**
- `_migration_245_restore_pattern_585` — one-shot restore (idempotent, safety-belted to `id=585 AND lifecycle='decayed' AND reason='thin_evidence_low_realized_wr' AND cpcv>=1.0`)
- `_migration_246_scan_pattern_payoff_ratio` — schema add (5 columns, IF NOT EXISTS each) + backfill from `trading_trades` aggregates

## Verification

**Tests.** `pytest tests/test_pattern_demote_payoff_ratio.py tests/test_pattern_demote_thresholds.py -v` → **25 passed in 1.34s**. All 9 new payoff-ratio tests pass; the 16 existing thin-evidence tests still pass (no regression).

**Deploy.** `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker` — all 5 services recreated.

**Post-deploy live DB observations** (probe in `scripts/dispatch-arch-postdeploy-2026-05-18-out.txt`):

| Check | Result |
|---|---|
| Pattern 585 lifecycle_stage | `decayed` → `pilot_promoted` ✓ |
| Pattern 585 demoted_at | `None` (cleared) ✓ |
| Pattern 585 promotion_demote_reason | `'thin_evidence_low_realized_wr'` → `NULL` ✓ |
| Pattern 585 payoff_ratio | **4.97** (winners +6.83%, losers −1.38% over n=86) ✓ |
| Pattern 585 payoff_ratio_updated_at | populated by mig 246 backfill ✓ |
| Lifecycle counts | pilot_promoted 11→12, decayed 2→1 ✓ |
| Patterns with `payoff_ratio` populated | 15 of 774 (= those with closed trades) |
| Patterns protected by payoff gate (≥1.5 AND n≥5) | **4** — pids 537, 585, 586, 1052 |
| Schema_version_tip query | failed with column-name typo (`version` vs `version_id`); migrations evidently fired regardless (585 changed, columns exist, backfill populated) |

**Surprise — and a significant one.** Pid 537 ("Falling Wedge Breakout + Trend Reclaim", `lifecycle_stage='challenged'`) has a **29.6:1 payoff ratio** over 7 trades (winners +4.81%, losers −0.16%). This was my #2 90d PnL contributor (+$85.96) but I missed it in the synthesis because I was anchored on pattern 585. **The audit just surfaced a likely second alpha.** With only 7 realized trades it's below the min-n floor for protection-by-payoff, but worth a Cowork strategy look at whether to promote it explicitly.

## Surprises / deviations

1. **Single commit instead of three.** I'd planned to split the work into 3 logical commits (mig 245 / mig 246+gate / composite floor) but the changes are tightly coupled — the migration adds columns the code reads, the realized_stats_sync extension feeds them, and the same `config.py` carries all three settings. One commit was cleaner and rolls back as a unit if needed.

2. **`schema_version.version_id` not `version`.** My post-deploy probe used the wrong column name. Real schema tip needs a follow-up query.

3. **537's payoff ratio is high enough that it likely IS a real second alpha**, not an artifact. The 7-trade sample size means I can't confirm it statistically yet (sign-test p ~0.06 if all 7 are winners). Recommend Cowork-write a NEXT_TASK to:
   - investigate 537's CPCV evidence,
   - if CPCV passes, promote 537 to `pilot_promoted` explicitly,
   - if CPCV is weak, queue 537 for fresh backtest.

4. **Composite-score floor showed zero immediate effect** because all 15 currently-scored patterns already have n ≥ 5. The floor will activate when a future pattern enters the composite pool — primarily preventing the cohort-promote landmine described in memory.

## Deferred

- **TCA wiring** (Tier B #4) — separate brief queued at `docs/STRATEGY/QUEUED/f-tca-writer-wiring.md`. Requires investigation into where `tca_entry_slippage_bps` SHOULD be written and why it isn't.
- **Position-identity Phase 2** (Tier B #5) — already in CURRENT_PLAN.md as the next-after-composite priority. Brief continues unchanged.
- **Pid 537 evaluation** — flagged above, needs a separate strategy decision.
- **`git push`** — pending operator (PROTOCOL Hard Rule blocks daemon-driven push to main).

## Open questions for Cowork

1. **Should I write a NEXT_TASK to evaluate pid 537?** Sign-test on 7 winners is statistically thin but the payoff ratio is compelling. Decision: investigate now vs wait for more trades.
2. **Should the `chili_composite_min_realized_trades` floor also propagate to the cohort-promote eligibility query?** Today the cohort-promote job already filters by `quality_composite_score IS NOT NULL`, so the floor naturally cascades. But a defense-in-depth check inside the cohort query itself might be wise.
3. **The `schema_version.version_id` column name** suggests the migration registration uses `version_id`. Not in scope here but worth double-checking that mig 245/246 actually wrote rows there (the 585 restoration is observable evidence they ran, but explicit confirmation would be cleaner).

## Rollback plan

If something behaves badly:

1. `git revert 23bde18` — reverts code; columns stay (idempotent).
2. To re-demote 585: `UPDATE scan_patterns SET lifecycle_stage='decayed', promotion_demote_reason='manual_rollback_2026_05_18' WHERE id=585`.
3. To disable the payoff-ratio protection without code revert: `CHILI_PATTERN_DEMOTE_PAYOFF_RATIO_FLOOR=1e9` in `.env`, then `docker compose up -d --force-recreate scheduler-worker brain-worker autotrader-worker`.
4. To disable the composite floor: `CHILI_COMPOSITE_MIN_REALIZED_TRADES=0`.

## Status

NEXT_TASK marked DONE (this was Cowork-direct execution, not a CC session, but the convention applies). Tier B briefs queued separately.
