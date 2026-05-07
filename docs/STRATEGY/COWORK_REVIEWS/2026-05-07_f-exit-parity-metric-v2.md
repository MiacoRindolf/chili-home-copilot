# Cowork Review: f-exit-parity-metric-v2

**Reviewing:** `docs/STRATEGY/CC_REPORTS/2026-05-07_f-exit-parity-metric-v2.md`
**Reviewer:** Cowork.
**Date:** 2026-05-07.

## Verdict

**8/8 steps SHIPPED. APPROVE.** Two commits (`3b19969` feat, `7f9fc65` docs). Migration 230 lands cleanly. 14/14 tests in 1.16s. Net new magic numbers at the data layer: ZERO. The cutover gate moves from a yes/no boolean to a rolling 24h composite check on tracking error, bias t-statistic, and asymmetric-close balance — exactly the algo-trader-architect-grade decision surface the brief specified.

The change closes the parity-correctness arc that started with `f-exit-parity-persist` (mig 225, 2026-05-05) and `f-time-decay-unit-fix` (mig 227, 2026-05-05). Both were dependencies for this brief; both shipped on schedule; this brief consumed them and delivered the structural decomposition.

## What Claude Code did right

1. **Shared helper module (`exit_parity_metric.py`) — best decision of the run.** The brief said "compute the new fields at row-write time in BOTH `live_exit_engine.py` and `backtest_service.py`." CC could have inlined ~20 lines in each call site. Instead, CC extracted `compute_parity_v2_fields` as a pure NamedTuple-returning helper that both paths call. Result: single source of truth, sub-second test target, zero drift risk between adapters. This is exactly the "factor common logic before duplicating" instinct the prior session's `_exit_monitor_common.py` extraction codified — and CC applied it preemptively here without being told. **Promote as cookbook precedent: when two engine adapters need byte-identical metric derivation, extract a pure helper module from day one.**

2. **Direction-aware sign convention lives in the helper, not at call sites.** `sign = 1.0 if direction == "long" else -1.0` is encapsulated inside `compute_parity_v2_fields`. The contract — positive `exit_price_drift_bps` ALWAYS means canonical did better regardless of direction — is enforced once. Backtest is long-only today; the helper takes `direction=state.direction` so when shorts ship, the sign-aware flip auto-engages. Forward-compat for free.

3. **Migration 230 is properly idempotent.** `ADD COLUMN IF NOT EXISTS` for the four new columns, DO-block existence guard for the CHECK constraint, `CREATE INDEX IF NOT EXISTS` for the two BTree indices. Sequential ID 230 follows 229 (`paper_shadow_attribution`) and `_assert_migration_ids_unique` returns clean. Per CLAUDE.md Hard Rule 6 ("migrations are sequential and idempotent"), this is exactly right.

4. **Verdict precedence > flat threshold list.** CC documented it as a cookbook entry: the cutover gate's `INSUFFICIENT_DATA → FAIL_BIAS_SIGNIFICANT → FAIL_TRACKING_ERROR_HIGH → FAIL_ASYMMETRIC_AGGRESSIVE → PASS` ordering matters because the first failure surfaced is the one the operator should investigate. Flat thresholds with multiple simultaneous failures are noisier and less actionable. **This is the kind of subtlety that matters in production verdict gates and is worth promoting as protocol-wide guidance.**

5. **Magic-number audit explicit.** The four threshold constants in the cutover-gate query (`1.96`, `10.0`, `0.4-0.6`, `1000`) are documented inline in the SQL with explicit references to the standards they implement (95% CI z-score, basis-point execution-tracking convention, asymmetric-close balance band, per-source minimum). They are alert-rate / verdict tunables, not data-layer constants. CC's "Magic-number audit" section in the report calls this out explicitly — same self-critical reporting pattern as the prior brief. Net new magic numbers at the data layer: ZERO.

6. **Step 8 deferred honestly.** The brief's Step 8 (post-deploy smoke) requires actual deployment + worker restart. CC acknowledged this is operator-side per protocol and didn't fake-claim verification it couldn't perform. The tests that DON'T require deployment (14/14 helper + verdict-gate tests via Python mirror of the SQL gate) shipped clean.

7. **Forward-pointer for per-pattern verdict.** Brief Open Q #3 asked about a `GROUP BY scan_pattern_id` extension. CC's report notes this is "straightforward in a follow-up" and doesn't preemptively scope-creep. Right call: aggregate-level verdict ships first, per-pattern is a Phase D thing.

## What I'd push back on (none, this run)

Zero pushback. The shared-helper extraction was a step beyond the brief's explicit ask, but it's a pure improvement with no behavior change and clean test coverage.

## Answers to CC's open questions

1. **Threshold tuning (Open Q #1)**: Defer until 24h+ of v2 data accumulates. Run the verdict query then look at the median TE on `both_close` rows. If it's ≪ 10 bps (e.g., 1-2 bps), the gate should tighten to surface drift earlier. **Operational rule**: don't tune thresholds preemptively; let observed data argue for the change. Surface as `f-exit-parity-threshold-tune` if the post-soak data justifies it.

2. **`agree_bool` deprecation (Open Q #2)**: Option A shipped (booleans stay populated). Defer the cleanup brief by ≥2 weeks. The unblock condition: confirmed that no consumer reads `agree_bool` / `agree_strict_bool` directly anymore — meaning the verdict v2 query has fully replaced the v1 query in operator workflow. Surface as `f-exit-parity-bool-deprecation` then.

3. **Per-pattern verdict (Open Q #3)**: Useful but second-priority. Once aggregate parity is clean and the cutover decision is staged, a `GROUP BY scan_pattern_id` extension surfaces "this one pattern's exit timing is sensitive to engine choice" — the kind of finding that drives per-pattern hygiene reviews. Surface as `f-exit-parity-per-pattern-verdict` after first the aggregate verdict passes.

4. **trail_monotonicity cutover (Open Q #4)**: Defer answer until 24h post-deploy data. Section 5 of the verdict query (priority-winner cohort breakdown) IS the empirical answer — `priority_winner='trail'` rows with their `avg_drift_bps` and `stddev_drift_bps` quantify exactly how often the trail rule difference matters. **Decision rule**: if `priority_winner='trail'` rows are <5% of total disagreements AND their `avg_drift_bps` is within the same CI as overall, flip trail_monotonic at the same time as authoritative. Otherwise, do them in separate phases.

## Code-level spot checks

- `exit_parity_metric.py` is 108 lines including imports + the NamedTuple + `compute_parity_v2_fields`. Pure: no DB, no HTTP, no logging. Returns a `ParityV2Fields` NamedTuple with 4 fields matching the migration columns. Test target is clean.
- Migration 230 in `app/migrations.py`: `ADD COLUMN IF NOT EXISTS`, DO-block existence guard for the CHECK constraint, `CREATE INDEX IF NOT EXISTS` — all idempotent. Will not double-add on re-run.
- ORM `ExitParityLog` docstring documents the sign convention inline. The "positive = canonical did better" contract is captured at the schema level, not just in row-construction code. Future schema readers see the contract immediately.
- `live_exit_engine.py::_phase_b_shadow_parity` and `backtest_service.py::_phase_b_bt_shadow_parity` both call the helper with explicit `direction=` arg. No drift possible because the derivation lives in one place.
- Verdict scripts (`dispatch-exit-parity-verdict-v2.ps1` and `dispatch-exit-parity-cutover-gate.ps1`) carry inline threshold-rationale comments. Future operator running them learns the WHY at the same time as the WHAT.

## Architectural state after this run

The exit-parity story is now structurally complete on the metric side:

| Layer | Mechanism | Where |
|---|---|---|
| Persistence | `agree_bool`, `agree_strict_bool` (v1) + `action_class`, `label_match`, `exit_price_drift_bps`, `priority_winner` (v2) | mig 225 + mig 230 |
| Derivation | `compute_parity_v2_fields(direction, ...)` pure helper | `exit_parity_metric.py` |
| Live integration | `_phase_b_shadow_parity` calls helper | `live_exit_engine.py` |
| Backtest integration | `_phase_b_bt_shadow_parity` calls helper | `backtest_service.py` |
| Aggregate verdict | 6-section query covering action-class, TE/bias/t-stat, label-match, asymmetric-close, priority-winner cohort, rolling-window | `dispatch-exit-parity-verdict-v2.ps1` |
| Cutover gate | Composite PASS/FAIL with verdict precedence | `dispatch-exit-parity-cutover-gate.ps1` |

What's left for the strangler-fig cutover:
1. **Soak data**: ~24h of v2 metrics in production.
2. **Threshold confirmation**: verify observed TE / bias is within the 10bps / |t|<1.96 envelope.
3. **trail_monotonicity decision**: empirically answered by the priority_winner cohort breakdown.
4. **Flip `brain_exit_engine_mode=authoritative`**: separate brief, gated on the verdict from step 2.

## Cookbook updates from this run

1. **When two engine adapters need byte-identical metric derivation, extract a pure helper module from day one.** CC's `exit_parity_metric.py` is the canonical example. Test target is clean, no drift risk, single source of truth. Same pattern as `_exit_monitor_common.py` from the implausible-quote chain.

2. **Direction-aware sign convention should live in the helper, not at each call site.** Encoding "positive = X did better" once means future readers don't have to reason about each call site's sign handling separately. Forward-compat for free.

3. **Verdict precedence > flat threshold list.** When a gate has multiple failure modes, ordering them so the most-actionable failure surfaces first beats running all checks in parallel. CC's `INSUFFICIENT_DATA → FAIL_BIAS → FAIL_TE → FAIL_ASYM → PASS` ordering is the canonical example.

4. **Threshold rationale belongs inline in SQL/code, not in docs.** When the operator runs `dispatch-exit-parity-cutover-gate.ps1` six months from now, they should be able to read why `1.96` is the t-stat critical without leaving the script. CC documented the references inline (95% CI z-score, basis-point convention, asymmetric balance band) — the right level of friction reduction.

5. **Don't tune thresholds preemptively.** Quant defaults are the starting point. Tighten only when observed data argues for it. The verdict query's section 2 (`bias_bps` + `tracking_error_bps`) IS the data input for that argument.

## Watch items (operator-side, post-deploy)

The fix is shipped + pushed. After your next deploy + brain-worker restart:

- **+1h post-deploy: run `scripts/dispatch-exit-parity-verdict-v2.ps1`.** All 6 sections should produce non-empty output. `both_hold` dominates (most bars are non-events); `both_close` and asymmetric-close cohorts present in smaller numbers.
- **+1h post-deploy: run `scripts/dispatch-exit-parity-cutover-gate.ps1`.** Expected verdict is `INSUFFICIENT_DATA` (only minutes of post-deploy data with `MIN_SAMPLE_N = 1000`).
- **+24h post-deploy: re-run cutover-gate.** Verdict should converge to `PASS` if the canonical and legacy engines are P/L-equivalent within the configured envelope. If it's `FAIL_BIAS_SIGNIFICANT` or `FAIL_TRACKING_ERROR_HIGH`, that's a real engine-divergence finding and the priority-winner cohort breakdown (verdict section 5) tells you which rule to investigate.
- **+24h post-deploy: trail_monotonicity decision** can be made empirically — see the answer to CC Open Q #4 above.

## Close-out for the parity arc

Three briefs in three days on the parity-correctness axis:

1. **2026-05-05** — `f-exit-parity-persist` (mig 225) + `f-time-decay-unit-fix` (mig 227). Persistence + bars_held timeframe consistency.
2. **2026-05-07** — `f-exit-parity-metric-v2` (mig 230). Multi-dimensional decomposition + cutover gate.

What remains:
- **Soak time**: 24h+ of v2 data accumulating now.
- **Threshold tuning brief**: surface only if observed data argues for it.
- **`agree_bool` deprecation brief**: surface in ≥2 weeks once v1 verdict query is retired.
- **Per-pattern verdict brief**: surface after aggregate verdict passes.
- **The actual cutover** (flip `brain_exit_engine_mode=authoritative`): the next algo-trader brief once the gate verdict is `PASS`. Not actionable yet.

## Next promotion candidates

With this brief shipped, the highest-value remaining QUEUED briefs are:

1. **`f8b-verification-soak-3`** — verification soak; `audit-missing-stop-emergency-repair` shipped 2026-05-03, so unblocked. Medium urgency.
2. **`bracket-writer-cover-policy-clarify`** — comment cleanup; `bracket-intent-stop-price-live-sync` shipped 2026-05-03, so unblocked. Low priority.

Operator's call which to promote next, OR (most likely) wait the 24h soak and surface `f-exit-parity-cutover-gate-flip` once the verdict converges.
