# NEXT_TASK: f-position-identity-phase-5l-d-dirty-evidence-reader-slice

STATUS: PENDING

## Goal

Move the remaining dirty evidence/model reader candidates from the legacy
compatibility view name to the semantic management-envelope relation without
absorbing unrelated local edits.

Phase 5L-C converted the clean candidates (`crypto/pattern_miner.py` and
`options/portfolio_budget.py`) and reduced the reader allowlist. The next
candidates are still legitimate, but the files are already dirty in the
worktree.

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

Fresh evidence after Phase 5L-C:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Reader/options focused tests: 22 passed
```

## Work Shape

1. Inspect dirty local diffs before editing:
   - `app/services/trading/pattern_regime_ledger.py`
   - `app/services/trading/pattern_survival/features.py`
2. Convert only the raw reader relation names to `MANAGEMENT_ENVELOPES_RELATION`.
3. Use isolated staging or blob-level staging so unrelated dirty edits are not
   committed.
4. Update the Phase 5L reader allowlist to remove converted lines.
5. Re-run:
   - targeted tests for touched modules/canary
   - Phase 5K-A parity probe
   - Phase 5I post-rename soak probe
6. Leave broker/order/reconcile writer paths alone unless they are migrated
   behind explicit envelope/position APIs.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class in this phase.
- Do not search-replace writer/order/broker/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Do not make the allow-list broad enough to hide new live-reader drift.
- `pattern_regime_ledger.py` and `pattern_survival/features.py` are dirty local
  candidates; inspect before touching and do not absorb unrelated edits.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity
