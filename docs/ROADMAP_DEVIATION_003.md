# Roadmap deviation 003 — Q1.T2 regime classifier (Gaussian HMM)

## Migration ID

- Megaprompt example used **093**; this repository’s `MIGRATIONS` tail after CPCV work ends at **164**. Q1.T2 ships as **`165_regime_snapshot_and_tagging`** in [`app/migrations.py`](../app/migrations.py).
- Run `.\scripts\verify-migration-ids.ps1` before merge.

## hmmlearn pin

- Megaprompt suggested `hmmlearn==0.3.3`. Verified on **Python 3.11** with **NumPy 2.x** and **scikit-learn 1.8.x** in `chili-env` (import + `GaussianHMM.fit` smoke test). Pin remains **`hmmlearn==0.3.3`** unless a future bump fails that matrix.

## Yield-curve feature (no FRED in repo)

- The megaprompt named **DGS10 − DGS2 (FRED)**. This codebase has **no FRED ingestion**.
- Q1.T2 uses **`trading_macro_regime_snapshots.yield_curve_slope_proxy`** (Phase L.17 ETF-based proxy) joined **by `as_of_date`** for point-in-time yield slope. Dates without a macro row omit yield and are **skipped** in the feature matrix (see runbook).
- If full Treasury term-structure features are required later, add a dedicated ingestion PR (e.g. **Q1.T2a**) rather than bolting FRED into the classifier module.

## Snapshot table name

- ORM **`MarketSnapshot`** maps to table **`trading_snapshots`** (not `trading_snapshot`).

## Persisted labels and posteriors (shipped behavior)

- **`regime_snapshot.regime`** and **`regime_snapshot.posterior`** are produced together by **`regime_and_posterior_for_sequence`** in [`app/services/trading/regime_classifier.py`](../app/services/trading/regime_classifier.py): one **`model.score_samples(X)`** on the **full** feature matrix so each row’s marginal over hidden states matches the transition structure of the fitted HMM.
- **`regime`** is **`argmax(posterior)`** for that row (human-readable bull/chop/bear via **`relabel_by_mean_return`**). This avoids the bug where **`model.decode(X)`** (Viterbi path) was paired with **`score_samples` on a length-1 sub-sequence** (different quantity → bogus “bear” + near-1 “bull” in the dict).
- **Invariant:** every persisted row satisfies **`posterior[regime] >= 0.5 * max(posterior.values())`** (see **`assert_regime_posterior_row_consistent`**). Backfill runs a short sample assert after fit; **`test_regime_label_posterior_consistency`** is the CI regression guard.
- **Diagnostics:** logs still include a **Viterbi path** label distribution for comparison; persisted rows follow the **marginal argmax** distribution.
- **Cold vs warm start:** optional **`CHILI_REGIME_FORCE_COLD_FIT=true`** (Settings: **`chili_regime_force_cold_fit`**) skips loading **`regime_models/`** for warm-start during weekly retrain and **`scripts/backfill_regime.py`**. Use for quarterly sanity refresh (see runbook).

## Rollback (manual)

```sql
DROP INDEX IF EXISTS ix_trading_snapshot_regime;
ALTER TABLE trading_snapshots DROP COLUMN IF EXISTS regime_posterior;
ALTER TABLE trading_snapshots DROP COLUMN IF EXISTS regime;
DROP INDEX IF EXISTS ix_regime_snapshot_model_version;
DROP TABLE IF EXISTS regime_snapshot;
```

(Index `ix_trading_snapshot_regime` is on `(regime, bar_start_at)` where `regime` is not null.)

## Tech debt (follow-ups)

### Pattern promotion vs CPCV evidence asymmetry (Q1.T2.5)

Many **`promoted` / `live`** `scan_patterns` rows have **little or no** usable history in **`trading_pattern_trades`** (PTR) for CPCV (e.g. zero rows after joins, or &lt; minimum labeled count after triple-barrier labeling). The **legacy ensemble / promotion path** (non-CPCV) can still elevate patterns to promoted/live, while the **CPCV gate** cannot score them until PTR-backed evidence exists.

**Implication:** With **`CHILI_CPCV_PROMOTION_GATE_ENABLED=false`**, CPCV metrics may be missing or skipped for those rows; with the flag **on** in a future PR, **new** promotions that fail CPCV would be blocked, but **existing** promoted/live patterns without CPCV evidence sit in a **gray zone** (not auto-demoted by this stack unless a backfill or policy says so).

**Open questions (deferred post–Q1.T1.5):**

1. Should promoted/live rows **without** CPCV evidence be **auto-demoted** (or flagged) when enforcement flips on?
2. Should **new** promotions require CPCV-eligible evidence from day one, with patterns **accumulating** PTR in **`validated`** (or similar) until they pass CPCV?

Documenting now so flipping the flag does not surprise operators; no behavior change in this note.

- **Yield slope proxy drift:** The regime classifier consumes **`yield_curve_slope_proxy`** from Phase L.17 macro snapshots, not a real **DGS10 − DGS2** (FRED) series. If regime label quality looks noisy in the **first ~30 days** of shadow operation, treat proxy drift vs. true curve slope as a **likely** cause and investigate before chasing HMM hyperparameters. Replacing the proxy with a real FRED feed becomes a ticket **when** label quality materially matters for gates or research.
- **T2 full-upsert parity test vs. DB contention:** A full upsert parity test for regime tagging is **deferred** where CI/shared DB contention makes deterministic fixtures flaky. **`test_flag_off_is_noop`** guards the highest-risk path (flag off ⇒ no writes). Revisit a full parity test when contention is resolved or an isolated DB fixture is available.
