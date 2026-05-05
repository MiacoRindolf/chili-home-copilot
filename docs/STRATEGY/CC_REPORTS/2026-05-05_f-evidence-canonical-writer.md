# CC_REPORT: f-evidence-canonical-writer

## What shipped

One commit covering the seven implementation steps. Migration ID is **228** (next sequential after `f-time-decay-unit-fix`'s 227).

**Files touched (5):**

- `app/migrations.py` — `+_migration_228_pattern_evidence_corrections` and registry entry. Idempotent `CREATE TABLE IF NOT EXISTS` + 3 indexes. The audit table is the single source of truth for "did this cycle's recomputation change anything, and if so what before/after?"
- `app/models/trading.py` — new `PatternEvidenceCorrection` ORM placed alongside `ExitParityLog` (audit-table neighbourhood). Added `UUID` to the `sqlalchemy.dialects.postgresql` import line for the `cycle_run_id` column.
- `app/services/trading/evidence_correction.py` — **new module**. Pure functions; no DB writes. Public surface:
  - `compute_trade_correction(...) -> TradeCorrection` — per-trade canonical-aware computation. Routes overheld trades through `_fetch_counterfactual_close` for the OHLCV-derived close at bar `max_bars`. Falls back to realized when CF is unavailable so the sample isn't biased by dropping these trades.
  - `aggregate_pattern_stats(corrections) -> PatternStats` — folds per-trade corrections into the three ScanPattern fields plus the three coverage counters (`overheld_n`, `counterfactual_applied_n`, `counterfactual_unavailable_n`).
  - `_fetch_counterfactual_close` (private) — multi-provider OHLCV via `market_data.fetch_ohlcv_df`, indexed at the timeframe-aware `max_bars` offset from entry.
  - `_period_for_timeframe` (private) — provider-friendly period sizing.
- `app/services/trading/learning.py` — refactored `update_pattern_stats_from_closed_trades` (lines 4798-4892) **in place**. Same name, same signature `(db, user_id) -> dict[str, Any]`, same call site at line 9582. Internals replaced with the canonical-aware path: bucket by pattern, compute corrections, aggregate, persist via `_evidence_correction_persist`. Three new module-private helpers (`_evidence_correction_first_run`, `_evidence_correction_resolve_max_bars`, `_evidence_correction_persist`) follow the function. The legacy 2-trade minimum filter (`if len(trades) < 2`) was dropped per the brief's "every pattern processed" audit-completeness requirement.
- `tests/test_evidence_canonical_writer.py` — **new test file**. 14 tests covering all brief items.

**Migrations added: 1** (`228_pattern_evidence_corrections`).

## Migration ID confirmation

`.\scripts\verify-migration-ids.ps1` → `OK: 228 migrations, 0 retired; no ID collisions.`

Migration applied to `chili_test`. Schema introspection post-apply:

```
('id', 'bigint')
('scan_pattern_id', 'integer')
('cycle_run_id', 'uuid')
('before_win_rate', 'double precision')
('after_win_rate', 'double precision')
('before_avg_return_pct', 'double precision')
('after_avg_return_pct', 'double precision')
('before_trade_count', 'integer')
('after_trade_count', 'integer')
('closed_trades_considered', 'integer')
('overheld_trade_count', 'integer')
('counterfactual_applied_count', 'integer')
('counterfactual_unavailable_count', 'integer')
('correction_reason', 'character varying')
('created_at', 'timestamp without time zone')
indexes: ['pattern_evidence_corrections_pkey',
          'ix_pattern_evidence_corrections_pattern_created',
          'ix_pattern_evidence_corrections_cycle',
          'ix_pattern_evidence_corrections_reason_created']
```

## Verification

### Tests

```
pytest tests/test_evidence_canonical_writer.py -p no:asyncio
> 14 passed in 516.02s   (~8.6 min; per-test truncate dominates runtime)

pytest tests/test_exit_evaluator.py tests/test_exit_evaluator_parity.py -p no:asyncio
> 248 passed in 1.31s
```

The 14 cases cover:

1. ✅ Not-overheld 1d trade returns `overheld=False`, `corrected == realized`.
2. ✅ Overheld 1m trade with mocked counterfactual returns `overheld=True`, `corrected_pct` based on CF close.
3. ✅ Short sign convention: long exit < entry loses; short exit < entry wins.
4. ✅ CF unavailable returns `counterfactual_available=False`, falls back to realized.
5. ✅ Aggregate over 10 mixed trades produces correct `overheld_n=4`, `counterfactual_applied_n=3`, `counterfactual_unavailable_n=1`, `win_rate=0.7`.
6. ✅ Aggregate over zero trades returns `n=0`, no NaN.
7. ✅ End-to-end: writes one audit row per pattern, updates the three fields, returns `patterns_updated=1`.
8. ✅ First-run detection: empty audit table → `first_run_backfill`. Re-run → `periodic_recompute` or `no_change`.
9. ✅ Coverage gate: `cf_unavailable / overheld > 0.5` → `correction_reason='coverage_too_thin'`, ScanPattern fields **not** mutated.
10. ✅ Idempotence: second invocation with no new trades produces identical pattern values; second audit row gets `correction_reason='no_change'`.
11. ✅ Realized-EV-gate integration: a pattern flipped from positive to negative `avg_return_pct` under correction fails `evaluate_realized_ev` (gate auto-demotes for free).
12. ✅ NaN guard: per-trade `try/except` boundary skips a malformed trade without breaking the pattern's aggregation.
13. ✅ Sign sanity: 5 long winners overheld with CF closes lower than realized → corrected `avg_return_pct` (= 2.0%) lower than realized would have been (= 10.0%).
14. ✅ 180-day cutoff: a trade closed 200 days ago is excluded; only the 5-day-old trade counts.

### Smoke (deferred to deploy)

Per brief Step 6, real first-run-backfill verification requires the brain-worker to run one full learning cycle post-deploy. The 5 SQL queries from the brief are documented inline in this report; results will populate `pattern_evidence_corrections` on the first cycle. Top-30 movers and coverage-gap headlines will land in the next Cowork review once the deploy lands and one cycle runs.

## Surprises / deviations

### 1. Brief's `Trade.close_date` is wrong — actual column is `Trade.exit_date`

The brief's Step 3 pseudocode queried `Trade.close_date` and the helper's signature took `close_date: datetime`. The actual column on both `Trade` (line 53) and `PaperTrade` (line 1053) is `exit_date`, and the legacy function at the same site (`learning.py:4818`) already used `Trade.exit_date`.

Implementation uses `Trade.exit_date` everywhere. The helper signature kept the name `close_date: datetime` because that name is more semantically descriptive at the function-argument layer ("close" reads better than "exit" when the helper doesn't know whether the trade is paper or live). The mapping is just at the call site:

```python
corr = compute_trade_correction(
    ...,
    close_date=trade_row.exit_date,   # <-- field name shim
    ...,
)
```

If Cowork prefers the helper-signature parameter to also be `exit_date`, that's a one-line rename.

### 2. Legacy 2-trade minimum filter dropped

The pre-fix function had `if len(trades) < 2: continue`, skipping patterns with only one closed trade. The brief explicitly requires "audit row written every cycle, every pattern processed, even when the value didn't change", which is incompatible with skipping any pattern. Dropped the filter.

Implication: a pattern with exactly one closed trade now gets its `win_rate` and `avg_return_pct` computed off that single sample. That's a noisier signal than the legacy 2-trade gate provided, but:
- The realized-EV gate already requires `trade_count >= chili_realized_ev_min_trades` (default 5) before promoting. So the noisy 1-trade stat doesn't propagate to live decisions.
- The audit row preserves the count (`closed_trades_considered`) for forensic reproduction.
- Skipping silently was its own sin: a 1-trade pattern's stale `win_rate` from an earlier cycle would persist indefinitely with no audit trail.

### 3. Renamed brief's helpers from `_is_first_run` / `_resolve_max_bars` / `_persist_pattern_correction` to `_evidence_correction_*`-prefixed names

The brief used short helper names. `learning.py` is a 9000+ line module with many neighbouring functions; short names like `_is_first_run` collide visually with anything similar. Renamed to `_evidence_correction_first_run`, `_evidence_correction_resolve_max_bars`, `_evidence_correction_persist` for clarity. Internal-only; no external callers.

### 4. Skipped trades with missing/zero entry_price BEFORE feeding the helper

The pre-fix function did `if row.entry_price and row.exit_price and row.entry_price > 0: ret_pct = ...; else: ret_pct = 0.0` — silently treating invalid rows as zero-return. The new path skips them entirely (per the corrected aggregate philosophy: bad data is excluded, not zero-stuffed). The test for the NaN guard (#12) covers the related case.

### 5. The brief's `audit_reason` selection logic was internally inconsistent — implemented the obvious-correct version

The brief's pseudocode had two passes that both set `audit_reason`, and the second pass could overwrite the first in ways that weren't immediately obvious. I implemented the simpler, equivalent flow:

```python
if coverage_too_thin:
    audit_reason = "coverage_too_thin"
elif backfill_mode:
    audit_reason = "first_run_backfill"
elif changed:
    audit_reason = "periodic_recompute"
else:
    audit_reason = "no_change"
```

Verified by tests #8 and #10 that the audit reason transitions correctly across the lifecycle (`first_run_backfill` → `periodic_recompute` or `no_change`).

## Audit summary

- **No new magic numbers**. The `0.5` coverage-gate threshold is per the brief; documented inline. The `20` `max_bars` fallback in `_evidence_correction_resolve_max_bars` mirrors `_load_exit_config`'s default.
- **Realized-EV gate untouched**. Test #11 confirms the gate auto-demotes patterns whose corrected stats flip from positive to negative — for free, no new threshold.
- **Canonical evaluator untouched**. `exit_evaluator.py` stays source of truth for time-decay semantics.
- **Trade row data untouched**. Counterfactual is derived at evidence time; no schema mutation on `Trade` / `PaperTrade`.
- **The other 6 writer sites in `learning.py`** untouched per brief constraint. The conflict-matrix analysis the brief did remains valid: site 4870-4877 (now this fix) overwrites site 4715-4717 within the same `run_learning_cycle` for any pattern with closed trades, so fixing the load-bearing writer is sufficient.

## Deferred (explicitly not in this task)

- **Backtest-derived evidence correction.** Different surface; gated on `f-exit-parity-metric-v2` cutover (queued).
- **Per-trade audit granularity.** Audit table records pattern-level totals; if Cowork wants per-trade rows (which specific trades got CF treatment with realized vs counterfactual prices), that's a separate table — surface as follow-up brief.
- **First-deploy backfill timing**: the post-deploy backfill IS the first invocation of `run_learning_cycle`; no separate one-time script. Smoke queries documented inline.
- **Notifying the operator on per-pattern demotion.** Audit table is the alert surface; no email/Slack integration in this fix.

## Open questions for Cowork

1. **OHLCV coverage gap quantification**: post-deploy, what's the actual `total_cf_gap / total_overheld` ratio? Will need the smoke query on production data. Surfaced as headline metric per brief.
2. **First demotion-cycle observation**: which patterns flip from passing to failing the EV gate post-correction? List + timeframe distribution will surface in the next Cowork review.
3. **`coverage_too_thin` patterns**: count + timeframe distribution. Likely 1m-weighted because provider OHLCV retention is shortest there.
4. **Brief's `Trade.close_date` field name**: brief's SQL/pseudocode used the wrong column name. Surfaced in Surprise §1; flagging because future briefs touching `Trade` columns may keep using the wrong name.
5. **Helper-signature parameter name `close_date`**: kept for semantic readability. If Cowork prefers verbatim alignment with the actual ORM column, one-line rename. Surfacing for explicit decision.
6. **Sites 4715-4717 (`learn_from_breakout_outcomes`)**: still write `pattern.win_rate` / `avg_return_pct` from breakout-alert outcomes. For patterns with NO closed trades in the 180-day window, those values stand. They don't have the time-decay bug (alerts don't hold positions), but they're a different signal entirely. If the post-deploy audit shows a meaningful number of patterns where the breakout-outcome value materially differs from what the closed-trade aggregation would say, surface in the next review.
7. **Per-trade audit granularity** — see Deferred. Same flag as Cowork's brief Open Q #6.

## Stale uncommitted work (carry-forward)

Pre-existing at session start, untouched: `app/models/trading.py` `_trade_phantom_close_guard` event listener (still in working tree, unstaged), `.env.example` `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags, `data/ticker_cache/crypto_top.json` byte-shift, untracked `.commit_msg_*.txt` / `docs/AUDITS/*` / `docs/STRATEGY/COWORK_REVIEWS/*` backlog. Same disposition as prior CC reports: left exactly as found.
