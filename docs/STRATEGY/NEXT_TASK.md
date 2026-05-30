# NEXT_TASK: f-position-identity-phase-5l-b-reader-allowlist-canary

STATUS: PENDING

## Goal

Add a guardrail that prevents new raw live-reader SQL from being introduced
against the `trading_trades` compatibility view.

Phase 5L-A moved the two low-risk reader candidates (`attribution_service.py`
closed-pattern stats and `cost_aware_gate.py` TCA usable-sample counts) to the
semantic management-envelope relation. The next risk is drift: future code
adding another direct `FROM trading_trades` live reader.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Live flags currently promoted:

- `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`
- `CHILI_PHASE5K_PDT_USE_ENVELOPES=true`
- `CHILI_PHASE5K_COHORT_PROMOTE_USE_ENVELOPES=true`
- `CHILI_PHASE5K_PATTERN_QUALITY_USE_ENVELOPES=true`
- `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=true`
- `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=true`
- `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=true`

Fresh evidence after Phase 5L-A:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
```

## Work Shape

1. Add a focused test/canary that scans runtime Python files for raw live-reader
   SQL using `FROM trading_trades` or `JOIN trading_trades`.
2. Build an explicit allow-list for:
   - migrations
   - tests
   - docs/scripts/history
   - compatibility contracts
   - writer/order/broker/reconcile paths
   - trade-id semantic readers that still need a separate API migration
3. Ensure the canary fails for any new non-allow-listed live reader.
4. Leave broker/order/reconcile writer paths alone unless they are migrated
   behind explicit envelope/position APIs.
5. Re-run Phase 5K-A and Phase 5I.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class in this phase.
- Do not search-replace writer/order/broker/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Do not make the allow-list broad enough to hide new live-reader drift.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity
