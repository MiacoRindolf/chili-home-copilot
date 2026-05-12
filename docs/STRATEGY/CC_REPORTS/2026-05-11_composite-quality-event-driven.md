# CC_REPORT: f-composite-quality-event-driven (Phase 3)

**Date:** 2026-05-11
**Brief:** `docs/STRATEGY/QUEUED/f-composite-quality-event-driven.md`
**Parent:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
**Plan-gate session:** `scripts/_claude_session_consult/composite-quality-event-driven-2026-05-11/`

## What shipped

* **New handler** `app/services/trading/brain_work/handlers/quality_score.py`
  with two entry functions (`handle_backtest_completed_quality`,
  `handle_trade_closed_quality`). Reuses
  `pattern_quality_score.compute_quality_composite_score`; never
  rewrites the math. Conditional-write idempotency contract: writes
  only when the recomputed score differs from the persisted value.
  Emits `pattern_quality_recomputed` (event_kind=`outcome`) when the
  score changes. Failure-swallowing at the handler boundary so
  upstream cpcv_gate / pattern_stats / regime_ledger commits survive
  a broken composite.
* **Dispatcher wiring** in
  `app/services/trading/brain_work/dispatcher.py`: one extra call in
  the `backtest_completed` branch (after `cpcv_gate`) and one extra
  call in the trade-close fanout (after `regime_ledger`). Each call
  is wrapped in its own try/except so a broken composite cannot
  poison the upstream chain.
* **Handlers re-export** in
  `app/services/trading/brain_work/handlers/__init__.py` (satisfies
  brief D2 without inventing a registry abstraction).
* **Streaming backfill wrapper**
  `compute_and_persist_scores_streaming` in
  `app/services/trading/pattern_quality_score.py` — reuses the pure
  `compute_quality_composite_score`. Batched commits, kill-flag
  polling between batches, optional per-pattern callback, dry-run
  mode (rolls back per batch instead of committing).
* **PowerShell backfill script** `scripts/quality-score-backfill.ps1`
  with `-DryRun` defaulting to `$true`, kill switch via
  `scripts/quality-score-backfill-stop.flag`, optional
  `-VerboseProgress` for per-pattern logs at
  `scripts/quality-score-backfill-progress.log`. Invokes the streaming
  wrapper inside the `chili` container via `docker compose exec`.
* **Adaptive gate 4D Pareto** in
  `app/services/trading/cpcv_adaptive_gate.py`:
  * `_load_pool_metrics` now also reads
    `scan_patterns.quality_composite_score` and exposes a
    `pool["composite"]` array.
  * `_evaluate_adaptive` adds a 4th metric row for `composite`. NULL
    candidates impute pool_mean (Q1 default) and are eligible by
    default during the backfill window. Non-NULL candidates compare
    to the empirical `q=0.95` percentile of the pool composite
    array; below threshold → `adaptive_composite_below_pool_threshold`.
  * `_pareto_dominated` is now generic over tuple width; the pool
    triplet → quad and candidate triplet → quad. Pool members with
    NULL composite are filled with pool_mean so the 4-D comparison is
    well-defined.
  * Wrapper `maybe_apply_adaptive_gate` reads the candidate's
    composite from `scan_patterns` via one indexed lookup, threading
    it into `eval_payload`. `promotion_gate.py` is untouched per the
    brief's hard constraint.
  * Shadow log writes one extra row per evaluation (4 metric rows +
    1 summary row); the existing `cpcv_adaptive_eval_log` table
    accepts arbitrary `metric_name` values — no migration needed.
* **Tests** (`tests/test_handler_quality_score.py`, 9 tests):
  idempotency on no-change, write-on-change, NULL-when-evidence-missing,
  NULL-when-thin-directional, skip-retired, missing-pattern,
  backtest_completed payload shape, trade-close event shape, and
  inner-exception-swallowed. Mock-based, mirrors Phase 1b's
  `test_brain_work_handler_idempotency.py` scaffold.
* **Tests** (`tests/test_cpcv_adaptive_gate.py`, +2 tests): composite-
  axis Pareto domination + NULL-imputed-to-pool-mean. Existing
  `test_shadow_log_writes_metric_and_summary_rows` updated to expect
  the new composite metric row.
* **Runbook** `docs/runbooks/QUALITY_SCORE_HANDLER.md`: handler
  semantics, score interpretation, backfill operations, rollback,
  4D-axis interaction, known limitations.
* This report.

Files touched: 4 production (`brain_work/handlers/quality_score.py`
new; `brain_work/handlers/__init__.py`,
`brain_work/dispatcher.py`, `pattern_quality_score.py`,
`cpcv_adaptive_gate.py` edited). 2 test files (1 new, 1 edited).
1 new script. 1 new runbook. No migrations added — column already
exists (mig 237); shadow-log table accepts arbitrary metric names.

## Consult-gated decisions

The plan-gate consult ran on 2026-05-11T21:54 and was reviewed
autonomously by Cowork at 21:55 (`plan.response.md`); the autonomous
review flagged 4 deviations as exceeding its threshold and recommended
interactive Cowork confirmation. The operator approved the plan
verbatim ("PLAN APPROVED + REQUEUED") and explicitly noted the two
consult-gate Q&As (NULL→pool_mean; event_kind=outcome) as
pre-approved. Executed exactly per the approved plan; no new
deviations beyond what `plan.request.md` already covered.

The four pre-approved deviations:

### 1. Subscribed event types (REVISED to match reality)

Brief named `pattern_stats_updated` and `regime_evidence_updated` —
neither has ever been emitted (postgres `brain_work_events` history).
Substituted with the four event types that actually fire AND modify
the inputs to `compute_quality_composite_score`: `backtest_completed`,
`live_trade_closed`, `paper_trade_closed`, `broker_fill_closed`. No
emitters added for the non-existent types (which would have required
touching existing handlers, forbidden by the brief's hard constraint).

### 2. Wiring location

`handlers/__init__.py` was a 19-line docstring with no registry.
Re-exported the two entry functions per the brief's literal D2 and
wired the dispatcher's per-event-type branches per the existing
idiom. The dispatcher is the orchestrator, not an "existing handler",
so the hard constraint "no changes to existing handlers" does not
gate dispatcher edits.

### 3. Backfill: streaming wrapper vs script-internal compute

Added `compute_and_persist_scores_streaming` (~110 lines) alongside
the existing `compute_and_persist_scores`. The nightly cohort path
is unchanged. The script reuses the same pure compute; no math
duplicated. Operator-callable kill switch + per-pattern progress
callback.

### 3-bis. Wrapper reads composite from DB

The brief's "add composite as 4th Pareto axis" plus its hard
constraint "no changes to promotion_gate" forced this. The wrapper
reads `scan_patterns.quality_composite_score` for the candidate
pattern inside `maybe_apply_adaptive_gate` itself (one indexed
lookup per gate call). `promotion_gate.py` is byte-identical.

## Verification

* AST parse on every modified `.py` file — PASS.
* Smoke import of the new handler module + handlers package + adaptive
  gate `_pareto_dominated` 4-tuple variant in chili-env — PASS.
* Test runs (`TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test`,
  `-p no:asyncio` workaround per Phase 1b note):
  * `tests/test_handler_quality_score.py` (9 new) —
    **9 passed in 0.87s**.
  * `tests/test_cpcv_adaptive_gate.py` (22 total; 20 existing + 2
    new composite-axis) — **22 passed in 277.02s**.
  * `tests/test_brain_work_handler_idempotency.py` (9 existing) —
    **9 passed**.
  * `tests/test_pattern_cohort_promote.py` (21 existing) —
    **21 passed** — confirms the existing nightly cohort path is
    byte-identical (the streaming wrapper is additive).
  * Combined regression run (idempotency + cohort): **30 passed in
    766.31s (~12m 46s)**.
* One existing test (`test_shadow_log_writes_metric_and_summary_rows`)
  updated to expect the new composite metric row in the shadow log;
  no behavioral surprise — the assertion change is mechanical and
  matches the additive shadow-log schema.

## Surprises / deviations

* Test scaffold gotcha: the `_FakeSession`'s `execute(...).fetchone()`
  initially returned the SAME first queue element on each call,
  because each `execute` returned a fresh MagicMock with a fresh
  `side_effect` list. Fixed with a shared queue + index counter so
  the directional-WR and decay queries get distinct rows. Phase 1b's
  scaffold doesn't hit this because no Phase 1b handler does multiple
  `execute().fetchone()` chains in a single call — quality_score
  does, and the scaffold needs to model that. Documented in the test
  helper's docstring.
* No additional code-level deviations encountered.
* The brief noted the composite-recompute is informational, so the
  handler swallows exceptions. The dispatcher wraps each call in its
  own try/except too, giving belt-and-suspenders containment. This
  matches the precedent of `pattern_stats`, `live_drift`, and
  `execution_robustness` exception handling in the dispatcher.

## Deferred

* **Backfill run.** Operator owns the script. The script is in the
  repo with `-DryRun:$true` default; running it is a separate
  operational step (per the brief's hard constraint "No backfill
  UPDATE runs — operator owns the script").
* **Per-handler kill switch.** Documented in the runbook; the only
  current blanket disable is Phase 1b's
  `chili_brain_outcome_claimable_enabled=False`. A future enhancement
  would add `chili_brain_quality_score_handler_enabled`.
* **Production flag flip of `chili_cpcv_adaptive_gate_enabled`.** The
  4D axis is computed and shadow-logged whether the flag is on or
  off; the verdict is only used when the flag is on. Operator-owned
  flip, post-merge.
* **`pending_by_type` UI alignment.** Phase 4 of the parent
  initiative.

## Open questions for Cowork

None blocking. The four pre-approved deviations + the two pre-
approved consult-gate Q&As (Q1 NULL→pool_mean; Q2 event_kind=outcome)
covered everything that came up.

Two soft items for future-Cowork consideration (not blocking
this phase):

1. **Per-handler kill switch flag.** The brief's hard constraint
   forbade touching `promotion_gate.py` but allowed the additive
   adaptive-gate edit. A future small phase to add
   `chili_brain_quality_score_handler_enabled` would give ops a
   surgical revert path without flipping the broader
   `chili_brain_outcome_claimable_enabled` (which would gate all
   outcome handlers, not just quality_score).
2. **Composite emit dedupe cadence.** The handler emits a
   `pattern_quality_recomputed` outcome whenever the score changes.
   In a burst (many trade closes in one dispatch round), the same
   pattern can emit multiple `quality_recomputed` events. The dedupe
   key includes the rounded new-score so identical-score re-emits
   collapse, but distinct intermediate values do not. If the outcome
   ledger gets noisy, a future debounce (similar to
   `enqueue_or_refresh_debounced_work`) would be the right shape.

## Commits (planned)

Three logical commits per the approved plan (let bisect isolate
handler-vs-gate-vs-backfill regressions independently):

1. `feat(brain): quality_score handler + dispatcher wiring (Phase 3)`
2. `feat(brain): cpcv_adaptive_gate 4D Pareto with composite axis`
3. `feat(brain): quality-score-backfill.ps1 + runbook (Phase 3)`
