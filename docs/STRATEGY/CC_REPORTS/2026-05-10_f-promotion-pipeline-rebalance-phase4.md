# CC_REPORT: f-promotion-pipeline-rebalance — Phase 4 (composite quality scoring + cohort auto-promote)

**Note**: This report is written by interactive Cowork (not CC) because CC's
session was killed mid-flight after the Edit-tool truncated 8 unrelated
large files. The brain-side intended Phase 4 work was clean and matched
the plan-gate-approved bindings exactly. Cowork salvaged it directly.
See COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase4.md
for the full incident.

## Outcome

Phase 4 shipped: composite quality scoring + weekly cohort
auto-promote. Patterns are now scored nightly on a 5-component
convex combination of CPCV / DSR / PBO / directional-WR / decay,
all clipped to [0,1] so composite ∈ [0,1]. The weekly Sunday-22:00-PT
cohort job advances top-N candidates (capped at 10 per rolling-7-day
window) to the `shadow_promoted` lifecycle stage (Phase 3's stage),
NOT to `promoted`/`live`. Risk-asymmetric: new candidates observe via
the Phase 2 directional-WR evaluator before earning live trading.

**Phase 4 ships dormant**: `chili_cohort_promote_enabled=False` by
default. Operator opts in by setting True. Until then:
- The score-refresh job runs nightly and populates the column
  (informational; operator can dry-run-inspect what cohort promote
  WOULD select).
- The cohort-promote job short-circuits at the flag check and
  doesn't advance any patterns.

## Per-step status

### Step 1 — Migration 237 — SHIPPED

`app/migrations.py` (+28 lines):
- `_migration_237_scan_pattern_quality_composite_score`: adds
  `quality_composite_score DOUBLE PRECISION NULL` to `scan_patterns`.
- Idempotent via `ADD COLUMN IF NOT EXISTS`.
- NULL is the correct initial state — patterns lacking required
  evidence stay NULL and are excluded from cohort eligibility by the
  column's NULL-check. NO magic default.
- Registered at position 237 in `MIGRATIONS`.

### Step 2 — Settings (8 new) — SHIPPED

`app/config.py` (+60 lines):
- `chili_cohort_promote_enabled: bool = False` (kill switch, default OFF)
- `chili_cohort_score_weight_cpcv_sharpe = 0.30`
- `chili_cohort_score_weight_deflated_sharpe = 0.20`
- `chili_cohort_score_weight_pbo_inverse = 0.15`
- `chili_cohort_score_weight_directional_wr = 0.25`
- `chili_cohort_score_weight_decay_inverse = 0.10`
- `chili_cohort_promote_top_n: int = 20`
- `chili_cohort_promote_max_per_week: int = 10`
Weights sum to 1.00 by default. Comment block documents the formula.

### Step 3 — Composite scoring module — SHIPPED

New file `app/services/trading/pattern_quality_score.py` (~310 lines):
- `compute_quality_composite_score(pat, directional_wr, decay, weights)`:
  pure function; returns `None` if any required component is `None`
  (NULL propagation, no magic-fallback).
- `_load_directional_quality_map(db)`: per-pattern WR + sample_n from
  `pattern_directional_quality_v` (Phase 2's view).
- `_load_decay_map(db)`: per-pattern decay from rolling-30 split.
  Splits 30 most-recent outcomes per pattern: newer-15 (rn 1-15),
  older-15 (rn 16-30). `decay = max(0, older_wr - newer_wr)`. Returns
  `None` when either half lacks 15 rows (pattern has insufficient
  evidence).
- `compute_and_persist_scores(db, *, settings_=None)`: idempotent
  batch run for all active patterns. Writes
  `quality_composite_score` (or NULL when components missing).

Eligibility tightening per Cowork plan-gate binding j.1: patterns with
`rolling_sample_n < 30` are excluded entirely (decay un-computable).

### Step 4 — Cohort promote module — SHIPPED

New file `app/services/trading/pattern_cohort_promote.py` (~210 lines):
- `select_cohort_candidates(db, *, settings_=None) -> list[ScanPattern]`:
  pure read; returns eligibility set ranked by score. SQL filter:
  ```sql
  active=TRUE
  AND lifecycle_stage IN ('backtested','candidate')
  AND promotion_gate_passed=TRUE
  AND cpcv_median_sharpe IS NOT NULL AND >= 1.0
  AND deflated_sharpe IS NOT NULL
  AND pbo IS NOT NULL
  AND quality_composite_score IS NOT NULL
  AND pdq.rolling_sample_n >= 30
  ORDER BY quality_composite_score DESC, id ASC
  LIMIT top_n
  ```
- `count_recent_cohort_promotions(db, *, since_hours=168) -> int`:
  counts ALL transitions to `shadow_promoted` in the rolling window
  (cohort-auto + operator-manual). Operator-manual moves count toward
  the cap.
- `run_cohort_promote_cycle(db, *, now=None, settings_=None) -> dict`:
  weekly entry point. Flag-gated by `chili_cohort_promote_enabled`
  (default False). Spots remaining = max(0, cap - recent count). If 0,
  short-circuits. Otherwise advances top-N (capped) to
  `shadow_promoted` and sets `lifecycle_changed_at`.

### Step 5 — Scheduler wiring — SHIPPED

`app/services/trading_scheduler.py` (+90 lines):
- `_run_pattern_quality_score_refresh_job`: nightly at 23:30 PT
  (CronTrigger, America/Los_Angeles tz). Always runs (no kill
  switch — score is informational). FIX 46 hygiene
  (rollback-before-close).
- `_run_pattern_cohort_promote_job`: weekly Sunday at 22:00 PT.
  Flag-gated at top — short-circuits when `chili_cohort_promote_enabled=False`.
  FIX 46 hygiene.
- Both registered with `replace_existing=True, max_instances=1`.

### Step 6 — Model column — SHIPPED

`app/models/trading.py` (+5 lines):
- `quality_composite_score: Optional[float] = Column(Float, nullable=True)`
  on `ScanPattern`.

### Step 7 — Tests — SHIPPED (21 tests)

New file `tests/test_pattern_cohort_promote.py` (~570 lines):

Pure / unit (no DB) — 11 tests:
- composite formula with full evidence
- pattern 585 calibration check (composite ≈ 0.843)
- NULL propagation: any of cpcv / dsr / pbo / wr / decay → None
- clipping: negative cpcv → 0; pbo > 1 → full overfit penalty
- operator-tuned weights actually shift score
- _clip helper bounds

Integration (DB; chili_test) — 10 tests:
- kill switch off → no advances (flag-disabled short-circuit)
- first-week promotes top-N capped by max_per_week
- eligibility excludes thin directional evidence (sample_n < 30)
- eligibility excludes below cpcv floor (< 1.0)
- eligibility excludes promotion_gate_passed False
- eligibility excludes already-shadow_promoted/promoted/live
- cap enforcement within rolling 7-day window
- tied scores → tiebreaker by id ASC
- idempotent within week
- compute_and_persist_scores populates column

## Verification

* AST parse clean on all 6 modified/new files.
* Pattern 585 calibration check: cpcv=1.4 → 0.7, dsr=1.0 → 1.0, pbo=0.0 → 1.0, wr=0.733, decay≈0 → 1.0. Composite = 0.30·0.7 + 0.20·1.0 + 0.15·1.0 + 0.25·0.733 + 0.10·1.0 = **0.843** (top tier). Matches Cowork's plan-gate calibration check.
* Migration 237 idempotency confirmed by code inspection (`ADD COLUMN IF NOT EXISTS`).
* Two scheduler jobs registered with appropriate cron expressions.

## Cowork plan-gate bindings observed (all 5)

- **j.1 decay metric**: newer-15 / older-15 split implemented exactly as
  Cowork bound. `decay = max(0, older_wr - newer_wr)`.
  `rolling_sample_n < 30` → decay=NULL → composite=NULL → excluded.
- **j.2 normalization**: `clip(cpcv/2.0, 0, 1)` and `clip(dsr/1.0, 0, 1)`
  implemented exactly. Composite ∈ [0,1].
- **j.3 audit trail**: `logger.info` per transition + `lifecycle_changed_at`
  column on `ScanPattern` (set on promote). No separate audit table.
- **j.4 cap window**: `count(WHERE lifecycle_changed_at >= now() - 7 days)`
  rolling, NOT ISO calendar week.
- **j.5 shadow_promoted exclusion**: not yet tested at runtime (no
  shadow_promoted patterns exist yet — Phase 4 ships dormant); will
  exercise once operator flips kill switch.

## Operator-side after Phase 4 ships

This commit does NOT trigger force-recreate. Phase 4 ships DORMANT
(flag default False); the running containers continue with HEAD code
as before until operator decides to deploy.

To deploy Phase 4 (when operator is ready):
1. `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker`
2. Verify mig 237 applied: `SELECT version_id FROM schema_version WHERE version_id LIKE '237%';`
3. Verify column exists: `\d scan_patterns | grep quality_composite_score`
4. Wait for first nightly score refresh (23:30 PT).
5. Inspect: `SELECT id, name, lifecycle_stage, quality_composite_score FROM scan_patterns WHERE quality_composite_score IS NOT NULL ORDER BY quality_composite_score DESC LIMIT 20;`
6. When ready, opt in: set `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env`, force-recreate, wait for next Sunday 22:00 PT cycle.

## Rollback plan

- Set `CHILI_COHORT_PROMOTE_ENABLED=false` in `.env`. Cohort job short-
  circuits at the flag check; no patterns advance. Score refresh job
  continues populating the column (non-destructive).
- Code revert: `git revert` the Phase 4 commit. The two new modules and
  scheduler hooks are removed. Migration 237 (`ADD COLUMN IF NOT EXISTS`)
  is intentionally left in place — harmless, just leaves an unused column.
- Already-cohort-promoted patterns at `shadow_promoted` stay there
  (Phase 3's lifecycle stage is independently valid).

## What's NEXT

- **Phase 5** — per-pattern universe via `scope_tickers`. `pattern_imminent_alerts.py` uses `pattern.scope_tickers ∩ tradable_universe` when scope_tickers non-null. Hard constraint baked into Phase 5's session prompt: use Write (not Edit) for files >500 lines.
- **Phase 6** — 7-day verification soak + final summary.

CC ships one phase per session per the brief's sequencing.
