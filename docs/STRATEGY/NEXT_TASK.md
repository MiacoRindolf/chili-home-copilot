# NEXT_TASK: f-position-identity-phase-5l-f-bracket-orphan-semantic-readers

STATUS: PENDING

## Goal

Wrap the remaining raw Phase 5L reader lines behind explicit semantic helper
contracts:

- bracket reconciliation readback/watchdog surfaces
- Coinbase orphan-adoption bracket join

After Phase 5L-E, the allowlist has only these exact runtime-app lines left.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Fresh safety evidence after Phase 5L-E:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Phase 5L reader allowlist: bracket reconciliation + Coinbase orphan only
```

## Recommended Work Shape

1. Inspect the remaining raw reader lines:
   - `app/services/trading/bracket_reconciliation_service.py`
   - `app/services/trading/venue/coinbase_orphan_adopt.py`
2. Classify each query by business meaning:
   - active bracket envelope readback
   - orphan stop/order adoption candidate
   - reconcile-only compatibility behavior
3. Introduce named helper APIs rather than inlining table names.
4. Convert only one sub-family at a time.
5. Add focused tests around the helper contract and update
   `tests/test_phase5l_reader_allowlist.py`.
6. Re-run Phase 5K-A, Phase 5I, and relevant bracket/orphan tests.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class.
- Do not search-replace broker/order/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Keep live close/order semantics unchanged.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity

## Architect Verdict

This is still safe to continue, but it is now high-risk surface area. Bracket
and orphan adoption are live broker-truth pathways. The right move is one small
semantic helper at a time with tests, not a mechanical rename.
