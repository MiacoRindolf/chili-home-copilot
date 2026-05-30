# NEXT_TASK: f-position-identity-phase-5l-contract-hardening

STATUS: PENDING

## Goal

Make the post-rename contracts explicit before any attempt to retire the
`trading_trades` compatibility view.

Phase 5K closed successfully: every safe aggregate live reader was moved behind
an envelope flag and promoted, and the final closeout audit found no remaining
blind single-reader cutover worth doing. The next risk is contract confusion,
not data parity.

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

Fresh evidence from the Phase 5K-I closeout:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
```

## Work Shape

1. Add a small management-envelope reader contract module or extend the existing
   helper module so common reads do not hand-write relation names.
2. Convert low-risk reporting/analytics readers that still directly read
   `trading_trades`, starting with:
   - `attribution_service.py` closed-pattern live stats
   - `cost_aware_gate.py` TCA usable-sample backing count
3. Add canaries that block new raw live-reader SQL against `trading_trades`
   outside allow-listed compatibility/migration/test/history files.
4. Leave broker/order/reconcile writer paths alone unless they are migrated
   behind explicit envelope/position APIs.
5. Re-run Phase 5K-A and Phase 5I after each small slice.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class in this phase.
- Do not search-replace writer/order/broker/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity
