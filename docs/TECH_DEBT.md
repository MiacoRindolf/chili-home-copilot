# CHILI Technical Debt Register

Living document. Issues that are known, scoped, and deferred — not bugs we're
unaware of. Each entry has a context, options, recommended path, and trigger
condition for acting on it.

---

## T2.5 — Pattern promotion vs CPCV evidence asymmetry

**Status:** open. Surfaced during Q1.T1.6 backfill rerun (2026-04-24).

### Context

The realized-PnL CPCV gate (`finalize_promotion_with_cpcv` in
`app/services/trading/promotion_gate.py`) requires at least
`chili_cpcv_min_trades` (default 15) closed `trading_pattern_trades` rows
with `outcome_return_pct IS NOT NULL` to evaluate a pattern. Patterns
without that evidence skip with `rows=0` reason and remain at their existing
`lifecycle_stage` regardless of the CPCV gate's behavior.

But the existing pre-CPCV promotion path
(`ensemble_promotion_check` → mining-time 3-segment heuristic, see
`brain_mining_purged_cpcv_enabled` in `app/services/trading/learning.py`)
does NOT require 15+ closed trades. A pattern can reach `promoted` /
`live` lifecycle stages on the strength of mining-time backtest evidence
alone, never accumulating live trade history sufficient for CPCV.

The Q1.T1.6 rerun confirmed this empirically: 28 promoted/live patterns,
27 with zero PTR rows (skipped CPCV), 1 with sufficient history (1047,
provisional pass).

### Why it matters

Today, with `CHILI_CPCV_PROMOTION_GATE_ENABLED=false`, this asymmetry is
informational. The CPCV gate is shadow-only, so pattern lifecycle is
unaffected.

The day operator flips `CHILI_CPCV_PROMOTION_GATE_ENABLED=true`, three
behaviors change simultaneously:

1. **New patterns:** at promotion attempt, must pass CPCV. Patterns with
   `<15` trades will be blocked with `n_trades_below_min`. Promotion
   pipeline will narrow significantly until trade history accrues.
2. **Existing promoted/live without CPCV evidence:** lifecycle stage is
   not auto-touched on flag flip. They continue trading; the gate only
   fires at *new* promotion attempts. The "gray zone" patterns persist
   indefinitely until they accumulate enough trades to be evaluated.
3. **Existing promoted/live with CPCV evidence already populated:** like
   pattern 1047, their gate state is already known. The flag flip just
   makes that gate state authoritative for future re-evaluations.

The asymmetry: long-promoted patterns get a free pass on CPCV simply
because the gate didn't exist when they were promoted. Newly-discovered
patterns face the full bar.

### Options

**Option A — Auto-demote on flag flip.** When the flag flips, scan all
`promoted`/`live` rows where `cpcv_n_paths IS NULL` and demote them to
`challenged` until they accumulate evidence and pass. Strict.

  Pros: gate semantics consistent across all patterns. No grandfathering.

  Cons: would demote ~27 of 28 currently-promoted patterns. Brain's live
  trading universe collapses overnight. Operator is forced into a "wait
  for trade history" period of weeks/months. The 1 evaluated pattern
  (1047) carries the entire promoted population alone.

**Option B — Freeze-as-grandfathered.** Pre-flag-flip patterns retain
their `lifecycle_stage` regardless of CPCV. Add a new column
`pattern_grandfathered_pre_cpcv: bool` (default FALSE) that the flip-day
backfill sets to TRUE for everything currently `promoted`/`live`. The
gate respects this flag — grandfathered patterns are never demoted on
sample-size grounds.

  Pros: trading continues; existing edge isn't lost. Honest about the
  history (the gate didn't exist; we're not pretending).

  Cons: creates a permanent two-tier population. Grandfathered patterns
  could be over-trading without CPCV-validated edge. Hard to remove the
  grandfathering later without creating Option-A-style disruption.

**Option C — Forced-evaluation pass with relaxed threshold.** When the
flag flips, run a one-time `force-evaluate` pass over all `promoted`/
`live` patterns with relaxed thresholds (e.g. `min_trades=5` instead of
15, accept `provisional` ratings on `n_paths < 20`). Patterns that
demonstrate any edge under relaxed CPCV are kept; patterns with no
evidence at all (rows=0) are demoted.

  Pros: discriminates between "no history at all" (demote) and "some
  evidence of edge" (keep). Less disruptive than Option A but more
  rigorous than Option B.

  Cons: requires defining the relaxed thresholds and defending them.
  Risk of false positives (a pattern with 7 trades and lucky DSR keeps
  promoted status it shouldn't). Adds a one-time-only code path that
  becomes dead code after the flip.

### Recommended path

**Option C, then a deprecation timeline toward Option A.**

  - Day 0 (flag flip): one-time forced-evaluation pass with
    `min_trades=5`, `n_paths_provisional_min=10`. Patterns with edge
    under relaxed CPCV stay; patterns with `rows=0` go to `challenged`.
  - +90 days: re-evaluate everything at full thresholds (`min_trades=15`,
    `n_paths_provisional_min=20`). Patterns that haven't accumulated
    enough live trades by then either get manually reviewed or auto-demoted.
  - +180 days: relaxed-evaluation code path removed. Permanent
    behavior reverts to Option A semantics.

This gives the brain time to build evidence on its own population while
not dropping all current promotions on day 1, and forces eventual
convergence to consistent semantics.

### Trigger condition

Act on this when `CHILI_CPCV_PROMOTION_GATE_ENABLED` is being prepared for
flip. Per the runbook readiness criteria (≥5 evaluated patterns with no
procedural failures), the flip is not imminent — weekly CPCV backfill
needs another 4-8 weeks to populate evidence on enough patterns. Defer
implementation until then.

### Open questions

- Should grandfathering be per-pattern (operator manually marks specific
  patterns as keep-anyway) or wholesale (everything pre-flag is grandfathered)?
- Should the relaxed-evaluation thresholds in Option C be configurable
  (env var) or hardcoded (with code-change-only override)?
- What's the right communication to the operator UI when grandfathered
  patterns appear? (Tag in `/brain` lifecycle counters? Separate
  "untested edge" column?)

---

## Yield curve slope: proxy → real FRED DGS10−DGS2

**Status:** open. Documented during Q1.T2 close-out.

The HMM regime classifier currently uses
`trading_macro_regime_snapshots.yield_curve_slope_proxy` as one of five
features. The proxy is a synthetic/internal signal, not the real
DGS10−DGS2 slope from FRED.

If regime label quality looks noisy or regimes shift in ways that don't
match observed macro reality, the proxy is the most likely culprit.

**Trigger condition:** if regime classifier produces obviously-wrong labels
(e.g. labels 2022 as `bull` instead of `bear`), or if regime-conditional
DSR data shows weak signal across all scanners, investigate the proxy first.

**Action when triggered:** Q1.T8a (currently slated for Q2 quick-wins) — add
real FRED ingestion + macro_yield_curve table + weekly job.

---

## T2 full-upsert parity test

**Status:** open. Skipped during Q1.T2 due to DB contention.

The regime classifier has unit tests covering label consistency and
backfill correctness, but the full upsert-parity test (verify that
`run_weekly_regime_retrain` and `backfill_regime --commit` produce
byte-identical outputs on the same input) was deferred because of DB
contention from concurrent test runs.

`test_flag_off_is_noop` guards the highest-risk path (no writes when
flag OFF). Full upsert parity is lower-risk but still worth shipping.

**Trigger condition:** CI test isolation work resolved (separate
`_test` DB per worker, or pytest-xdist with proper teardown).

**Action when triggered:** add `test_regime_upsert_parity` to
`tests/test_regime_classifier.py`.
