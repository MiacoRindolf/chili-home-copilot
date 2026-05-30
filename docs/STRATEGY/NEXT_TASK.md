# NEXT_TASK: f-position-identity-phase-5l-c-evidence-reader-slice-2

STATUS: PENDING

## Goal

Move the next clean evidence/model reader surfaces from the legacy compatibility
view name to the semantic management-envelope relation.

Phase 5L-B added a canary that blocks new raw live-reader SQL against
`trading_trades`. The next useful step is to reduce the known allowlist with
small, clean reader conversions.

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

Fresh evidence after Phase 5L-B:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Reader allowlist canary: 4 passed
```

## Work Shape

1. Start with clean direct SQL readers:
   - `app/services/trading/crypto/pattern_miner.py`
   - `app/services/trading/options/portfolio_budget.py`
2. Use `MANAGEMENT_ENVELOPES_RELATION` rather than hand-writing the relation.
3. Update the Phase 5L-B allowlist canary to remove converted lines.
4. Re-run:
   - targeted tests for touched modules/canary
   - Phase 5K-A parity probe
   - Phase 5I post-rename soak probe
5. Leave broker/order/reconcile writer paths alone unless they are migrated
   behind explicit envelope/position APIs.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class in this phase.
- Do not search-replace writer/order/broker/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Do not make the allow-list broad enough to hide new live-reader drift.
- `pattern_regime_ledger.py`, `pattern_survival/features.py`, and
  `pattern_survival/training.py` are dirty local candidates; inspect before
  touching and do not absorb unrelated edits.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity
