# NEXT_TASK: f-position-identity-phase-5l-h-relation-symbol-contracts

STATUS: PENDING

## Goal

Reduce the remaining runtime-app literal `trading_trades` relation-symbol
surface without touching broker/order/close behavior and without renaming the
legacy `Trade` ORM class.

Phase 5L-G proved there are no unexpected runtime raw readers or mutations.
The remaining ambiguity is the 17 app-side relation-symbol contracts.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Fresh safety evidence after Phase 5L-G:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Classifier: OK=True, unexpected runtime readers=0,
            unexpected runtime mutations=0, unclassified=0
```

## Recommended Work Shape

1. Run the Phase 5L-G classifier and list only
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
   - `tests/test_phase5l_reader_allowlist.py`
   - Phase 5K-A probe
   - Phase 5I probe

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class.
- Do not search-replace writer, broker, order, or reconcile code.
- Do not absorb unrelated dirty worktree files.
- Keep live close/order semantics unchanged.

## Architect Verdict

This should be a conservative clarity pass, not a behavior change. The system is
already safe from raw reader drift; now we make the remaining relation-symbol
surface easier to reason about before any future ORM naming discussion.
