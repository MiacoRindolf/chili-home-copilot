# CC_REPORT: f-canonical-outcome-layer (Phase A)

**Date:** 2026-05-15 (executed; brief dated 2026-05-14 — kept slug for
plan/response continuity)
**Brief:** `docs/STRATEGY/QUEUED/f-canonical-outcome-layer.md`
**Plan request:** `scripts/_claude_session_consult/canonical-outcome-layer-2026-05-14/plan.request.md`
**Plan response (APPROVED + 1 follow-up):** `scripts/_claude_session_consult/canonical-outcome-layer-2026-05-14/plan.response.md`

## What shipped

Phase A of `f-evidence-fidelity-architecture` — stops the silent race
between corrected and raw-realized pattern stats by splitting them into
distinct authoritative / shadow columns.

Files touched (10):

| File | Change |
|------|--------|
| `app/migrations.py` | New `_migration_241_scan_pattern_canonical_outcome_split` + `MIGRATIONS` entry. Adds 8 columns + 2 CHECK constraints (mirrors mig 193's `win_rate ∈ [0,1]`). Idempotent via `ADD COLUMN IF NOT EXISTS` + `pg_constraint` lookup (per Cowork PG≤16 note). |
| `app/models/trading.py` | 8 new `Column(...)` defs on `ScanPattern`. |
| `app/services/trading/learning.py` | `_evidence_correction_persist` now dual-writes `corrected_{trade_count, win_rate, avg_return_pct}` alongside the legacy columns and stamps `corrected_stats_updated_at`. |
| `app/services/trading/realized_stats_sync.py` | Rewrote the `UPDATE scan_patterns` to write **only** `raw_realized_*`. Added a `_shadow_log_divergence(...)` that emits INFO ≥ `chili_canonical_outcome_divergence_info_pct` (20 %), WARNING ≥ `chili_canonical_outcome_divergence_warn_pct` (50 %). No DB row, no metric — pure shadow log per brief consult-gate decision. |
| `app/services/trading/pattern_stats_accessor.py` | **NEW** ~75-line helper. `get_corrected_pattern_stats(pat)` reads corrected first, falls back to legacy when corrected is NULL. Single funnel for the "always-corrected, legacy is safety net" contract. |
| `app/services/trading/realized_ev_gate.py` | `evaluate_realized_ev` routed through the accessor; snapshot now records `stats_source_*` per field for auditability. |
| `app/services/trading/cpcv_adaptive_gate.py` | Pool-load SQL changed `COALESCE(trade_count, 0)` → `COALESCE(corrected_trade_count, trade_count, 0)` so the freshest corrected value wins in pool aggregates; legacy is the merge-window fallback. |
| `app/config.py` | Added `chili_canonical_outcome_divergence_info_pct` (0.20) and `chili_canonical_outcome_divergence_warn_pct` (0.50) settings. |
| `scripts/canonical-outcome-backfill.ps1` | **NEW** Two-pass backfill (Pass A primes corrected_* from current legacy; Pass B refreshes raw_realized_* via `sync_realized_stats`). `-DryRun:$true` default, kill-switch flag at `scripts/canonical-outcome-backfill-stop.flag`, idempotent. Logs a >20 %/>50 % divergence histogram. Runs in the chili container (matches `quality-score-backfill.ps1` shape). |
| `tests/test_canonical_outcome_layer.py` | **NEW** 4 race tests (see Verification). |

**Migration ID:** 241. Verified unique via `scripts/verify-migration-ids.ps1`
(241 migrations, 0 retired, no collisions).

## Verification

- `scripts/verify-migration-ids.ps1` → **PASS** (241 migrations, no
  collisions).
- AST parse of all 9 modified Python files → **PASS**.
- Import smoke (`pattern_stats_accessor`, `realized_ev_gate`,
  `realized_stats_sync`, `cpcv_adaptive_gate`, `learning`) → **PASS**.
- `tests/test_canonical_outcome_layer.py` (4 tests) → ran against
  `chili_test` (TEST_DATABASE_URL guard satisfied). **All 4 PASSED.**
  Tests 1–3 ran in a single session (~7 min total: 3 × ~2-min per-test
  TRUNCATE on Windows-Docker fsync, plus test bodies). Test 4 (pure
  accessor-helper logic) re-ran in isolation: `1 passed, 3 deselected,
  1 warning in 301.25s`. The slow per-test TRUNCATE is a known
  Windows-Docker fsync cost — `_truncate_app_tables` truncates 243 tables
  CASCADE per test; the wait_event in `pg_stat_activity` is consistently
  `DataFileImmediateSync` / `DataFileExtend`, not lock contention.
  Test coverage:
  1. **`test_corrected_writer_writes_legacy_and_corrected`** — corrected
     writer dual-writes legacy + corrected, stamps
     `corrected_stats_updated_at`. ✅ PASSED
  2. **`test_raw_writer_never_touches_legacy`** — raw writer leaves
     legacy + corrected unchanged; only `raw_realized_*` populated. ✅
     PASSED
  3. **`test_race_corrected_then_raw_leaves_legacy_corrected`** — fire
     corrected writer → snapshot legacy → fire raw writer with diverging
     trades → legacy still equals corrected snapshot. This is the
     pattern-585 race regression. ✅ PASSED
  4. **`test_accessor_prefers_corrected_with_legacy_fallback`** — helper
     read contract: missing → legacy → corrected as values populate. ✅
     PASSED

## Approved deviations (applied)

These three were flagged in the plan request and **explicitly approved**
in the plan response. They are shipped as-is:

### 1. Reader scope reduced from 5 → 2 files

The brief listed 5 reader files; only `realized_ev_gate.py` and
`cpcv_adaptive_gate.py` directly read `pattern.{win_rate, trade_count,
avg_return_pct}`. `promotion_gate.py` passes the `ScanPattern` object
through to `check_realized_ev_blocking` (no direct read);
`auto_trader.py` and `pattern_quality_score.py` consume different
metrics. Hardening only the two direct readers captures the entire
race-affected gate surface.

### 2. Legacy fallback in `realized_ev_gate` (merge-window safety)

The brief said "read corrected only". Reading corrected-only at merge
time would cause a **temporary promotion blackout** — every active
pattern has `corrected_*` = NULL until the dual-write fires (post-PR)
or the backfill runs. The accessor helper now resolves this in one
place: corrected first, fall back to legacy when corrected IS NULL.

**Merge-window semantics in detail:**

- T+0 (PR merges): every pattern has `corrected_* = NULL`. The accessor
  returns legacy values for every read; readers behave byte-identical
  to pre-PR (because legacy values are themselves unchanged — the raw
  writer no longer touches them, and the corrected writer hasn't run
  yet). **No promotion behavior change.**
- T+5 min (first learning cycle): the corrected writer fires once per
  pattern with closed trades; it dual-writes legacy + corrected.
  Post-cycle, the accessor returns corrected values for those rows. For
  patterns with no recent closed trades, corrected stays NULL → legacy
  fallback continues. Still safe.
- T+? (operator runs backfill once): Pass A copies legacy → corrected
  for any row still on legacy. After this, the accessor returns
  corrected for every row; the fallback is a no-op forever.

The fallback can be removed in a follow-up after 7 days of stable
operation if desired — not required.

### 3. `corrected_stats_updated_at` written by canonical writer only

Stamped inside `_evidence_correction_persist` (the corrected writer)
alongside the field mutation. Matches the existing audit-trail pattern.
The raw writer stamps its own `raw_realized_stats_updated_at`
separately.

## Operator-added follow-ups

- **Tiny `pattern_stats_accessor.py` helper.** Cowork suggested this as
  a small follow-up if <30-min effort. **Shipped.** ~75 LOC, both direct
  readers now route through it. Future readers have a single funnel.
- **Document legacy fallback in CC_REPORT.** Section "Approved
  deviation 2" above documents the merge-window semantics with concrete
  timeline.
- **Use `pg_constraint` lookup pattern in mig 241.** Done. Both new
  CHECK constraints (`chk_sp_corrected_wr_range`,
  `chk_sp_raw_realized_wr_range`) are wrapped in `DO $$ ...
  pg_constraint` existence checks, matching the convention in
  migrations 227, 225, 168, 167, 165, etc.

## Hard-constraint compliance

| Constraint | Status |
|------------|--------|
| Legacy columns stay populated (= corrected) | ✅ — `_evidence_correction_persist` dual-writes; raw writer no longer touches legacy |
| Migration is additive only | ✅ — 8 new nullable columns + 2 CHECK constraints; no `DROP`, no type change |
| Backfill `-DryRun:$true` default, idempotent, kill-switch | ✅ — `canonical-outcome-backfill.ps1` matches the `quality-score-backfill.ps1` shape; Pass A overwrite is value-equivalent on re-run |
| No autotrader / venue / broker behavior change at merge | ✅ — accessor returns legacy when corrected is NULL → byte-identical to pre-PR until the corrected writer fires the first time. The corrected writer's first run is a strict improvement (replaces an occasionally-raw legacy with always-corrected legacy). |
| TEST_DATABASE_URL must end in `_test` | ✅ — used `chili_test`; `conftest.py` guard satisfied |
| No magic numbers | ✅ — 20 %/50 % thresholds named `chili_canonical_outcome_divergence_info_pct` / `_warn_pct` |
| `pg_constraint` idempotency (no `ADD CONSTRAINT IF NOT EXISTS`) | ✅ — both CHECKs wrapped in `DO $$ ... pg_constraint` lookups |

## Surprises / nothing-significant deviations

- None beyond the three pre-approved deviations.
- Test file uses a minimal `_make_pattern` helper (only the columns the
  ScanPattern constructor actually requires); didn't try to mirror the
  full ORM defaults. Tests are self-contained.

## Deferred (out-of-scope per plan)

- **Tighten `realized_ev_gate` to corrected-only.** Once the backfill
  has run and at least 7 days of stable operation pass, the legacy
  fallback in `pattern_stats_accessor.py` can be removed. Mechanical
  one-line change.
- **Apply `pattern_stats_accessor.get_corrected_pattern_stats` to other
  consumers.** `alpha_decay.py`, `opportunity_scoring.py`,
  `regime_allocator.py`, `daily_playbook.py`, `scanner.py`,
  `stop_engine.py`, etc. still read `pattern.{win_rate, …}` directly.
  Brief explicitly endorsed leaving these on legacy because legacy is
  now always = corrected after dual-write fires.
- **Audit table for divergence > X %.** Brief default (shadow-log only)
  shipped. If Phase B–E reveal a structural divergence pattern, a
  `pattern_outcome_divergence` audit table can be added later.

## Open questions for Cowork

None. Plan was fully approved; all three flagged deviations were
accepted explicitly; the operator-added follow-up (the accessor
helper) was in scope and under the 30-min budget.

Phase A is foundational for B–E. Phases B (execution-truth-wiring)
through E (multiple-testing-discipline) can now proceed in parallel
against the new authoritative column surface.
