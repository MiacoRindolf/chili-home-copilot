# f-netedge-stage2-allocator-routing

> **Type:** Phase D Stage 2 of evidence-fidelity-architecture
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
> **Depends on:** Phase D Stage 1 (commit `e5a04e5`) — shadow-score wiring must accumulate calibration data BEFORE Stage 2 flips NetEdge to authoritative.

## Goal

Stage 1 (Phase D) wired a parallel shadow `net_edge.score(...)` call
inside `auto_trader._process_one_alert`. Every recent
`NetEdgeScoreLog` row now carries `scan_pattern_id` and `regime` —
the dataset NetEdge needs to be evaluable.

**Stage 2** routes the live autotrader THROUGH `portfolio_allocator.
evaluate(...)` so that NetEdge graduates from "shadow log next to the
heuristic gate" to "authoritative gate input." The allocator already
consumes NetEdge as part of its expectancy stack; the autotrader
currently bypasses it.

This is the cutover step. After Stage 2, NetEdge can:
- Block a pattern entry when expected net edge is negative
- Bias scale-in sizing toward higher-expected-edge alerts
- Surface a disagreement metric (heuristic-gate=YES vs NetEdge=NO)

## Hard prerequisite — calibration soak

**Stage 2 MUST NOT ship until:**

1. `NetEdgeScoreLog` has accumulated ≥ N rows with non-null
   `scan_pattern_id` AND non-null `regime`, where N is large enough
   that NetEdge's `_load_training_pairs()` returns a calibratable
   sample (use the existing `brain_net_edge_min_samples` setting,
   default 50, as the floor).

2. The regime-diagnostic added in Stage 1
   (`_maybe_emit_regime_diagnostic`) is NOT firing the >50%-unknown
   warning. This means upstream regime data is healthy.

3. Operator has reviewed a NetEdge calibration report (per-pattern,
   per-regime) and confirmed the scores have discriminatory power
   on the post-Stage-1 distribution.

A separate `f-netedge-stage1-soak-audit.md` brief should run the
calibration audit and produce a sign-off artifact before this
brief is promoted to NEXT_TASK.

## Design — what changes

### Wire-point: allocator-routing splice in `_process_one_alert`

Replace the current direct flow:

```
pattern_imminent_alert
  → rule_gate
  → LLM revalidation
  → _emit_netedge_shadow_score   (shadow only, Stage 1)
  → _execute_new_entry / _execute_scale_in
```

with:

```
pattern_imminent_alert
  → rule_gate
  → LLM revalidation
  → portfolio_allocator.evaluate(alert, regime, asset_class)
        |
        |── NetEdge.score() (authoritative — replaces shadow call)
        |── concentration cap check
        |── HRP sizing (when enabled)
        |── return Decision { proceed, sized_qty, score, blockers }
  → if not proceed: log skip + emit `[autotrader_blocked]` line + return
  → if proceed:   _execute_new_entry / _execute_scale_in(sized_qty)
```

### Settings

- `brain_net_edge_ranker_mode`: flip default from `"shadow"` to
  `"authoritative"` ONLY after Stage 1 soak signs off
- New `chili_autotrader_route_via_allocator_enabled` (default False)
  — flag-gated cutover so we can A/B Stage 1 shadow vs Stage 2 live
  during a parity window

### Parity logging

Emit `[autotrader_routing_parity]` on every alert during the cutover
window:
- `heuristic_decision={accept|skip}`
- `allocator_decision={accept|skip}`
- `disagreement={true|false}`
- `pattern_id`, `regime`, `asset_class`, `notional`

Use `chili_autotrader_routing_parity_log_enabled` (default True) to
turn this off after soak.

## Deliverables

D1. `app/services/trading/auto_trader.py` — splice
    `portfolio_allocator.evaluate(...)` between LLM revalidation and
    execution paths. Remove the parallel shadow call (it becomes the
    primary path).

D2. `app/services/trading/portfolio_allocator.py` — ensure
    `evaluate(...)` accepts the autotrader's alert shape (may already
    if Stage 1 used the same context structure). Audit for autotrader-
    specific edges (scale-in semantics, partial fills).

D3. Settings: `chili_autotrader_route_via_allocator_enabled` (default
    False during cutover, flipped True after soak)

D4. Settings flip: `brain_net_edge_ranker_mode` default from
    `"shadow"` → `"authoritative"` ONLY when Stage 1 soak audit gives
    sign-off

D5. Parity log emitter + `chili_autotrader_routing_parity_log_enabled`

D6. Tests:
    - `tests/test_netedge_autotrader_stage2_routing.py` — happy path,
      blocker case (allocator says skip → autotrader does not place),
      parity-log emission, flag-off byte-identical to Stage 1 behavior

D7. Migration: NONE required (parity log uses existing structured
    logging; no new tables)

D8. CC_REPORT documenting the cutover with the soak-audit artifact
    referenced as evidence

## Hard constraints

- Stage 2 is FLAG-GATED. With
  `chili_autotrader_route_via_allocator_enabled=False`, behavior is
  byte-identical to Stage 1
- Allocator-evaluation failure (any exception) must NOT block the
  autotrader — fall back to legacy path with a WARNING log
- Concentration caps and HRP sizing (when enabled) become hard
  constraints once the cutover flag is True
- No change to broker / venue adapters
- TEST_DATABASE_URL must end in `_test`
- Migration count must stay stable

## CONSULT GATE (escalate via plan-request)

1. **Sizing semantics for scale-ins.** Stage 1's shadow call ignored
   sizing; Stage 2 needs to honor allocator-sized qty. But scale-ins
   already have a sizing model in `auto_trader` itself. Reconcile:
   which wins? Brief default: allocator-sized for new entries;
   autotrader-sized for scale-ins (scale-in policy is preserved).

2. **Parity-window duration.** How long do we run with parity log on
   before flipping `chili_autotrader_route_via_allocator_enabled` to
   True? Brief default: 72 hours and >100 alerts processed without
   parity divergence rate exceeding 10%.

## After Stage 2

Stage 2 is the final piece of the evidence-fidelity-architecture
arc. Once it lands and the soak completes, the deferred items from
the parent brief are closed:

- ✅ Canonical outcome split (Phase A)
- ✅ Execution-truth wiring (Phase B)
- ✅ Triple-barrier label scheduler (Phase C)
- ✅ NetEdge live wiring Stage 1 (Phase D)
- ✅ Multiple-testing discipline (Phase E)
- → NetEdge Stage 2 (this brief)

Open follow-ups that remain operator-controlled (NOT in this brief):
- Hypothesis-family backfill on legacy patterns (separate dispatch)
- Roster-replay tool walking `pattern_family_trial_log` (Phase F
  proposal in Phase E CC_REPORT)
