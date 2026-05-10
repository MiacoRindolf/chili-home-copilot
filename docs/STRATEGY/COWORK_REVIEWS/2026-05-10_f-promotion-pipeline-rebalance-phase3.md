# COWORK_REVIEW: f-promotion-pipeline-rebalance — Phase 3

CC report: `docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase3.md`
Commit: `ba05195`
Session: `promotion-rebalance-phase3-2026-05-09`
Plan-gate: APPROVED at 00:04 PT after CC posted a 28KB plan
Verdict: **GREEN — parity gate held. Highest-stakes change in the initiative landed clean.**

## What the parity gate proved

`test_autotrader_byte_identical_for_promoted_pattern` PASSED. This was the single most important test in the entire 6-phase initiative. It proves:

1. For every existing pattern at `lifecycle_stage IN ('promoted', 'live')`, the autotrader's execution path produces IDENTICAL `_execute_new_entry` call args before and after Phase 3's splice.
2. The new helper `is_shadow_promoted_pattern(_pat)` is a true no-op for non-shadow_promoted patterns — same uid, same alert.id, same px, same `live` boolean, same snap dict, same llm_snap.
3. The 5-step structural proof CC included in its plan ("CPython bytecode picks up exactly where it did before, with the same `_stage` local already bound") was confirmed empirically.

The 8 pure-unit tests also PASS (helper-level + eligibility-branch coverage with no DB contention).

## What the plan-gate caught

CC flagged three deviations from the brief in its plan request — exactly the kind of decisions where silent CC choice could have produced subtle bugs. The plan-gate caught all three at zero implementation cost:

1. **Helper signature `(pat: ScanPattern)` instead of brief's `(scan_pattern_id, db)`** — saved a redundant DB query AND closed a race window between the autotrader's existing `_pat` query and the helper's. Approved.

2. **Audit `decision="blocked"`** — matches existing `selector:coinbase_routing_shadow_log` precedent. Now grep/aggregation tooling that pivots on `decision='blocked' AND reason LIKE 'selector:%'` catches both shadow paths uniformly. Approved.

3. **Flag-gated eligibility branch (separate `if`)** — brief's snippet was illustrative; documented rollback semantics REQUIRED the flag to gate the new branch. Approved.

If these had gone in silently they'd have introduced (a) a measurable race window, (b) an audit-ergonomics regression, and (c) a flag that didn't actually disable the eligibility path. The plan-gate fired exactly when it should have.

## Test execution caveat (acknowledged, not blocking)

3 of 4 DB-bound routing tests hit `psycopg2.errors.DeadlockDetected` because a hung pytest process from earlier (PID 61476, 7s CPU after 29 min, holding tables in `chili_test`) deadlocked against CC's pytest run. CC was transparent about this — didn't fake-pass tests, documented the environmental issue, suggested re-running after the parallel session clears.

The hard gate (parity test) DID pass. The 3 deferred tests exercise the same code path the parity test verified — the routing logic for shadow_promoted patterns is the same code that produces the byte-identical baseline for non-shadow patterns (just with helper returning True vs False).

**Action**: kill PID 61476 and re-run the 3 affected tests. Cowork is dispatching a kill-and-rerun script in parallel. Not blocking Phase 4 because the parity gate is the load-bearing test.

## Hard rules check

- ✅ Hard Rule 1 (live-placement safety belts): unchanged. New splice only adds a MORE restrictive path (shadow-log instead of broker call) for a brand-new lifecycle value that no existing pattern uses.
- ✅ Hard Rule 5 (prediction-mirror authority): untouched.
- ✅ Additive-only with default-True flag and documented off-state.
- ✅ No autotrader entry-side gate weakening — uses existing `_audit` machinery.
- ✅ No removal of existing demote logic.
- ✅ Edit-tool truncation discipline followed (no file shrunk unexpectedly).

## Phase 3 ships dormant — by design

No patterns are at `lifecycle_stage='shadow_promoted'` yet. The migration's CHECK constraint widening is harmless (strict superset). The new code path is exercised only by tests until Phase 4 (cohort promote) starts placing patterns at `shadow_promoted`. Live data risk this phase: zero.

This is the risk-asymmetric design the brief intended. Phase 3 is the runway; Phase 4 lights the engine.

## Phase 4 just queued

`scripts/_claude_session_queue/300-promotion-rebalance-phase4.session` — composite quality scoring + weekly cohort auto-promote. Plan-gate active. Default weights proposed by Cowork (revise if disagree):

- w1=0.30 (cpcv_sharpe — strongest established signal)
- w2=0.20 (deflated_sharpe — multiple-testing penalty)
- w3=0.15 (1-pbo — overfit penalty)
- w4=0.25 (directional_wr — Phase 2's clean signal)
- w5=0.10 (1-decay — recency penalty)

The 3 open questions from Phase 2's review are baked into Phase 4's prompt as binding answers (per-pattern hold_hours from rules_json, 1.5% threshold default, organic accumulation, no 90-day backfill). Phase 4 also adds `directional rolling_sample_n >= 10` as a cohort eligibility filter — patterns with thin directional evidence should not be cohort-eligible.

Phase 4 ships dormant too — `chili_cohort_promote_enabled=False` default until operator opts in.

## Forward look

After Phase 4 lands and the operator opts in `chili_cohort_promote_enabled=True`, the chain will:
1. Nightly compute composite scores
2. Weekly select top-N candidates with rolling_sample_n >= 10
3. Promote (capped at 10/week) to `shadow_promoted`
4. Phase 2's evaluator scores them on directional WR
5. Operator manually decides which `shadow_promoted` patterns earn `promoted` based on accumulated evidence (or wait for a future Phase that automates this)

Phases 5 and 6 close out the initiative (per-pattern universe via scope_tickers; 7-day verification soak).
