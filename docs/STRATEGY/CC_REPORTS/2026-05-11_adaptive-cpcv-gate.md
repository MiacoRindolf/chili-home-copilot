# CC_REPORT: f-adaptive-cpcv-gate (Phase 2 of f-adaptive-promotion-architecture)

Date: 2026-05-11
Session: `adaptive-cpcv-gate-2026-05-11`
Plan request: `scripts/_claude_session_consult/adaptive-cpcv-gate-2026-05-11/plan.request.md`
Plan response: APPROVED (autonomous Cowork, 2026-05-11T19:39:39Z)
Brief: `docs/STRATEGY/QUEUED/f-adaptive-cpcv-gate.md`
Parent: `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`

## What shipped

| Deliverable | Path | Status |
|---|---|---|
| D1 — Adaptive gate module | `app/services/trading/cpcv_adaptive_gate.py` | new (~480 lines) |
| D2 — Config fields | `app/config.py` (+16 lines) | 4 new pydantic settings |
| D3 — Migration 239 | `app/migrations.py` (+59 lines) | new table `cpcv_adaptive_eval_log` + index |
| D4 — Tests | `tests/test_cpcv_adaptive_gate.py` | 20 tests, all passing |
| D5 — Wiring | `app/services/trading/promotion_gate.py` (+19 lines) | single call site at line 982 inside `finalize_promotion_with_cpcv` |
| D6 — Runbook | `docs/runbooks/CPCV_ADAPTIVE_GATE.md` | operator playbook |
| D7 — This report | `docs/STRATEGY/CC_REPORTS/2026-05-11_adaptive-cpcv-gate.md` | |

Commit hash: filled in by the commit step below.

## Verification

### Tests (against `chili_test`)

```
TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
  pytest tests/test_cpcv_adaptive_gate.py -v -p no:asyncio
```

Result: **20 passed in 121s**.

Coverage breakdown:
- 2 × flag-off parity (with + without skipped payload)
- 3 × Bayesian shrinkage math (low-n, high-n, zero-n)
- 3 × empirical percentile (admit count, empty pool, single element)
- 3 × Pareto frontier (dominator, dominated, partial)
- 3 × portfolio marginal Sharpe proxy (positive, empty roster, negative)
- 2 × CI helpers (Hansen DSR, Wilson PBO — both shrink with more samples)
- 1 × shadow-log write (3 metric rows + 1 summary, both verdicts populated)
- 1 × flag-on adaptive verdict diverges from legacy under Pareto-dominated synthetic pool
- 2 × wrapper resilience (None payload, predicate type)

### Regression check

I stashed my changes and ran `tests/test_cpcv_promotion_gate.py` on the
pre-change baseline. `test_finalize_enforced_blocks` was already failing
on `main` (the realized_ev_gate appends `+realized_ev_gate_failed` to
the `detail["blocked"]` string, but the test expects the bare value).
Re-applying my changes did not change that test's failure mode. **No
new regressions introduced.**

### Static analysis

- AST-parse clean on all 5 changed Python files.
- `scripts/verify-migration-ids.ps1` → PASS (239 migrations, 0 retired,
  no ID collisions).
- Truncation scan (advisor brief §2.1):
  - `app/config.py`: +16 lines (matches the 4-field block I added)
  - `app/migrations.py`: +59 lines (matches mig 239 body + MIGRATIONS
    entry)
  - `app/services/trading/promotion_gate.py`: +19 lines (matches the
    single wrap block at line 982)

## Behaviour summary

**Default merge state:**
- `chili_cpcv_adaptive_gate_enabled = False`
- Wrapper computes both verdicts on every call to
  `finalize_promotion_with_cpcv` and writes the shadow log
  (`cpcv_adaptive_eval_log`).
- Wrapper returns the **legacy** `(ok_metrics, reasons)` tuple
  unchanged. Promotion behaviour is byte-identical to pre-merge.

**Operator-controlled rollout:**
1. After 7 days of shadow log accumulation, query the divergence rollup
   (see runbook §"Reading the shadow log").
2. If the adaptive verdicts look sound, flip
   `CHILI_CPCV_ADAPTIVE_GATE_ENABLED=1` (env or `trading_settings`).
3. Rollback = flip the flag back. The legacy verdict is always
   computed first and the wrapper is the only adaptive-aware call site.

## Operator-policy defaults (confirmed via plan-gate)

| Setting | Default | Reason |
|---|---|---|
| `chili_cpcv_target_promotion_pool_pct` | 0.05 | "Top ~5% of patterns live by each metric" — matches operator's pool-size policy in brief §"Open questions" |
| `chili_cpcv_ci_level` | 0.90 | "90% confidence in lower-bound" — standard CI convention |
| `chili_portfolio_marginal_sharpe_min_bps` | 0.0 | "Any positive contribution admits" — no-op floor at merge; tune up after shadow-log evidence |

All three documented in the runbook under the "policy not magic"
framing, with tuning direction + safe ranges.

## Surprises / deviations from brief

**Two flagged deviations, both approved by plan-gate:**

1. `TIMESTAMP` → `TIMESTAMPTZ` on `cpcv_adaptive_eval_log.evaluated_at`.
   Brief specified `TIMESTAMP`; mig 164 (`cpcv_shadow_eval_log`) uses
   `TIMESTAMPTZ`. Switched to `TIMESTAMPTZ` so operator-query patterns
   line up across both CPCV audit tables.

2. `marginal_portfolio_sharpe_bps` uses a lightweight proxy
   (`candidate_sharpe - mean(roster_sharpes)`) rather than a true
   covariance-aware portfolio marginal. Phase 2 doesn't have a
   per-roster returns matrix wired up at gate time. The proxy is
   directionally informative and shadow-logged only — the default
   `min_bps = 0.0` makes it a no-op floor. Documented in the runbook
   §"How the math works" and §"Open follow-ups" as a Phase 3+
   refinement.

## Deferred

- **Step 2 of the rollout sequence (7-day shadow soak).** Operator
  decision after observation; not shipped by this brief by design.
- **True covariance-aware portfolio marginal Sharpe.** Phase 3+
  refinement once the prediction-mirror exposes a per-pattern returns
  matrix at gate time.
- **`quality_composite_score` (mig 237) as a 4th Pareto axis.** Phase 3
  composite-quality event-driven node.
- **Pre-existing test failure** `test_finalize_enforced_blocks` —
  baseline failure unrelated to this brief; should be addressed by the
  owner of the `realized_ev_gate` integration (string-equality vs
  prefix-match on `detail["blocked"]`).

## Open questions for Cowork

1. **Should the wrapper try to commit its own shadow-log write?**
   Currently it calls `db.commit()` inside `_write_eval_log`, then
   falls back to `db.flush()` if commit fails (e.g. when the caller
   owns the transaction). This is safe in the test fixture and in
   the brain-worker handler call site, but a future caller that
   wants the shadow-log INSERT to be part of a larger transaction
   may want a separate "don't-commit" mode. Defer until a caller
   surfaces the need.

2. **Pool exclusion semantics.** `_load_pool_metrics` excludes the
   candidate pattern from its own pool stats when an id is provided.
   This is the correct statistical behaviour but means very-early
   patterns (when only a handful have CPCV data) get a degenerate
   pool. The empirical-percentile path returns `None` for empty pools
   and falls through to "no threshold" eligibility. Is that the right
   default for a still-cold pool, or should the wrapper fall back to
   legacy verdicts when the pool is below some minimum size?

## Files produced / modified

```
M app/config.py                                       (+16 lines)
M app/migrations.py                                   (+59 lines)
M app/services/trading/promotion_gate.py              (+19 lines)
A app/services/trading/cpcv_adaptive_gate.py          (new, ~480 lines)
A tests/test_cpcv_adaptive_gate.py                    (new, 20 tests)
A docs/runbooks/CPCV_ADAPTIVE_GATE.md                 (new)
A docs/STRATEGY/CC_REPORTS/2026-05-11_adaptive-cpcv-gate.md  (this file)
```

## Acceptance signal — all green

- [x] All 7 deliverables shipped (D1 module / D2 config / D3 migration /
      D4 tests / D5 single-site wiring / D6 runbook / D7 CC_REPORT).
- [x] Flag defaults `False`. Merge produces zero behaviour change.
- [x] `promotion_gate.promotion_gate_passes` not modified.
- [x] Wrapper is the SINGLE call site (verified by diff against
      `promotion_gate.py`).
- [x] Shadow-log table additive only. No `scan_patterns` column adds.
- [x] No autotrader / venue / broker touched.
- [x] All 20 new tests pass under `TEST_DATABASE_URL=...chili_test`.
- [x] AST-parse clean on all 5 changed Python files.
- [x] `verify-migration-ids.ps1` PASS.
- [x] Truncation scan clean (matches expected deltas).
