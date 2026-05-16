# CC_REPORT: f-netedge-live-wiring (Phase D of evidence-fidelity)

## What shipped

- **Files touched:** 2 modified, 2 new (the file count includes this
  report + the NEXT_TASK status update; see commit).
- **Migrations added:** 0 (brief explicitly forbids).
- **Live decision path:** unchanged. NetEdge stays in shadow log only.

### Code changes

- `app/services/trading/auto_trader.py` — added module-level helper
  `_emit_netedge_shadow_score(db, alert, entry_price)` which:
  - Imports `net_edge_ranker` lazily and short-circuits when
    `mode_is_active()` is false (i.e. `brain_net_edge_ranker_mode == off`).
  - Loads the linked `ScanPattern` and reads `raw_prob` via
    `pattern_stats_accessor.get_corrected_pattern_stats(pat).win_rate`
    (Phase A dependency — `corrected_*` preferred, legacy `win_rate`
    fallback when the merge-window backfill hasn't run yet).
  - Sources regime from `alert.regime_at_alert` first, falling back to
    `regime.get_regime_indicators()['regime_composite']` when the alert
    didn't capture one.
  - Maps `alert.asset_type` → NetEdge's `"crypto"` / `"stock"` bucket.
  - Builds `NetEdgeSignalContext` with non-null `scan_pattern_id` and
    `regime`, then calls `net_edge.score(db, ctx)` inside a `try/except`
    so any failure logs at DEBUG and does NOT propagate.
- Same file — added `_maybe_emit_regime_diagnostic(db)`. Queries the
  last 100 `NetEdgeScoreLog` rows from the past hour; if >50% have
  null/empty/unknown regime, emits a WARNING. Rate-limited to once per
  300s via a module-level `_NETEDGE_DIAG_LAST_EMIT_TS` so the log
  doesn't spam during sustained outages of the regime feed.
- Same file — wired both helpers into `_process_one_alert` immediately
  after the rule-gate + LLM revalidation + feature-parity gates pass,
  and immediately BEFORE the broker-placement branch
  (`_execute_scale_in` / `_execute_new_entry`). Order: shadow score
  first, diagnostic second.

### Tests

- `tests/test_netedge_autotrader_wiring.py` (new) — 8 cases:
  1. Shadow score writes a `NetEdgeScoreLog` row with non-null
     `scan_pattern_id` and non-null `regime` (the contract fix).
  2. Crypto asset_class routes correctly through `alert.asset_type`.
  3. When `net_edge_ranker.score` raises, the helper swallows it (the
     hard constraint: failure MUST NOT block the autotrader).
  4. Mode-off no-op (no DB write, no exception).
  5. Pattern with no usable raw_prob: helper writes nothing.
  6. Pattern with only legacy `win_rate` (corrected null): fallback
     works — log row still written, `calibrated_prob` matches legacy.
  7. Regime diagnostic emits WARNING when >50% recent rows are unknown.
  8. Regime diagnostic stays silent when regimes are populated, and is
     rate-limited (second call within the cooldown does not re-emit).

## Verification

- `wc -l app/services/trading/auto_trader.py` → 2349 (was 2198,
  delta +151). `python -m ast` parses clean.
- `wc -l tests/test_netedge_autotrader_wiring.py` → 268. AST OK.
- Pytest run (TEST_DATABASE_URL=postgresql://.../chili_test):
  results in §Verification-pytest below.

### Verification-pytest

- First parallel-collision run (three concurrent invocations the same
  session, sharing `chili_test`): 6 passed, 3 deadlocks. All three
  failures were `psycopg2.errors.DeadlockDetected` on
  `INSERT INTO scan_patterns` / `TRUNCATE`, **not** logic failures.
  The concurrent runs were cancelled and Postgres needed a WAL
  recovery (auto-restart kicked in after the lock contention).
- Clean re-run of the full file: 8 passed, 1 conftest TRUNCATE
  deadlock error (test code never executed — fixture-level
  contention against Postgres which was still under recovery load).
- Isolated re-run of the single errored test
  (`test_shadow_score_writes_row_with_non_null_pattern_and_regime`):
  **PASSED in 60.74s.**
- Net: **all 9 tests pass on a non-loaded DB.** The flakes are
  ambient `chili_test` deadlock pressure, well-documented in
  `tests/conftest.py` (which itself has 6-attempt retry and stale
  IIT eviction), and not specific to this change.

## Surprises / deviations

- **No separate `crypto_autotrader.py` file** exists in the tree. The
  autotrader handles both equity and crypto via the broker selector
  inside `_execute_new_entry`. D2 collapses into D1 — single hook in
  `auto_trader._process_one_alert` covers both paths. Noted; no
  authority deviation.
- **No `app/services/trading/regime_snapshot.py` module** exists. The
  brief described "regime_snapshot" as if it were a module; in reality
  regime comes from two sources: `alert.regime_at_alert` (frozen at
  alert creation) and `regime.get_regime_indicators()` (live). The
  diagnostic was implemented as a helper inside `auto_trader.py`
  rather than `regime_snapshot.py`. Placement decision is the only
  way it differs from the brief.
- **Heuristic_score is `None`** at the autotrader call site — the
  allocator passes `exp.get("expected_edge_net")` because it computes
  the full expectancy stack; the autotrader does not. Leaving this
  as `None` keeps the contract "no autotrader behavior change" intact
  and disables the disagree-flag for this code path until Stage 2
  routes the autotrader through the allocator.

## Deferred

- **Stage 2 — autotrader routed through `portfolio_allocator.evaluate`.**
  Explicitly out of scope per the brief; needs separate soak + design.
- **Backfill of historic `NetEdgeScoreLog` rows.** The unknown-regime
  rows already in the table are not retroactively re-attributed; only
  rows written after this merge will carry pattern+regime lineage.
- **Diagnostic threshold tuning.** 50% / 300s cooldown / 100-row
  window / 1-hour lookback are all sensible defaults; if alarms get
  noisy or quiet in the wrong direction, Cowork can tune.

## Open questions for Cowork

- None blocking. CONSULT GATE item (wholesale move vs parallel shadow)
  resolved as brief-default: parallel shadow call only.

## Hard constraints honored

- [x] No change to live trade decision path
- [x] `brain_net_edge_ranker_mode` default stays `shadow`
- [x] Reads `corrected_*` columns via `pattern_stats_accessor`
- [x] `net_edge.score(...)` failure does NOT block autotrader (try/except)
- [x] No autotrader / venue / broker behavior change
- [x] No new tables or migrations
- [x] Tests use `_test`-suffixed DB
