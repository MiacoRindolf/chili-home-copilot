# NEXT_TASK: f-position-identity-phase-5l-h-relation-symbol-contracts

STATUS: QUEUED

## Goal

Reduce the remaining runtime-app literal `trading_trades` relation-symbol
surface without touching broker/order/close behavior and without renaming the
legacy `Trade` ORM class.

Phase 5K/5L reader slices proved there are no unexpected runtime raw readers or
mutations. After Phase 5AK, `/api/trading/trades` is default-on for the
management-envelope route path, with `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES`
kept as an explicit rollback switch.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Fresh safety evidence:

```text
Phase 5AH: COMPLETE_POSITIVE, all/open/closed exact_match=true
Phase 5AG: COMPLETE_POSITIVE
Phase 5AE: COMPLETE_POSITIVE
Phase 5K:  COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:  COMPLETE_POSITIVE, 21 fresh decisions, 21 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
```

## Recommended Work Shape

1. Run the remaining-trade-reference classifier and list only
   `compatibility_relation_symbol` entries.
2. Split them into:
   - true compatibility constants / ORM metadata
   - raw writer/reconcile references that must stay on the view
   - low-risk comments or diagnostics that can name the semantic relation
3. Convert only low-risk relation-symbol references to shared constants or
   clearer wording.
4. Leave all live broker/order/close semantics unchanged.
5. Re-run:
   - `tests/test_phase5_remaining_trade_refs.py`
   - Phase 5AH, Phase 5K, and Phase 5I probes

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class.
- Do not search-replace writer, broker, order, close, or reconcile code.
- Do not absorb unrelated dirty live-root files.
- Keep live close/order semantics unchanged.
