# NEXT_TASK: f-position-identity-phase-5l-e-trade-id-semantic-reader-contracts

STATUS: PENDING

## Goal

Design and ship the next Phase 5L slice by moving remaining live
`trade_id`/`trading_trades` semantic readers behind named management-envelope
helper APIs instead of doing another mechanical table-name replacement.

Phase 5L-A through 5L-D removed the safe reader-only evidence/reporting
surfaces. The remaining runtime references are semantic: they reason about
management envelopes, retry windows, broker reconciliation, order placement, or
compatibility behavior.

## Current State

Physical relation state:

```text
trading_management_envelopes = physical base table
trading_trades               = legacy compatibility view
```

Fresh safety evidence after Phase 5L-D:

```text
Phase 5K-A: COMPLETE_POSITIVE, 6/6 checks, 0 mismatches
Phase 5I:   COMPLETE_POSITIVE, 20 fresh decisions, 20 fresh envelopes,
            10 fresh closes, 0 hard linkage issues, 0 attribution drift
Phase 5L reader allowlist: 1 passed
```

## Recommended Work Shape

1. Run the current remaining-reference classifier.
2. Pick one semantic-reader family, not a broad sweep. Recommended first family:
   - autotrader retry/probation/open-by-lane readers
   - bracket watchdog/readback surfaces
   - Coinbase orphan/adoption reads
3. Introduce named helper APIs in `management_envelopes.py` or a sibling module
   that express the business meaning, for example:
   - `count_open_management_envelopes_by_lane(...)`
   - `load_recent_envelope_attempts(...)`
   - `load_position_envelope_for_reconcile(...)`
4. Convert only callers whose semantics are covered by the helper.
5. Add tests that pin behavior against both the compatibility view and the base
   table where parity matters.
6. Re-run Phase 5K-A, Phase 5I, and the Phase 5L canary.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the `Trade` ORM class in this phase.
- Do not search-replace writer/order/broker/reconcile code.
- Do not absorb unrelated dirty worktree files.
- Treat writer/order/broker/reconcile paths as behavioral code requiring helper
  contracts and focused tests.
- Preserve the three-layer model:
  - `trading_decisions`: immutable entry intent
  - `trading_management_envelopes`: mutable management envelope
  - `trading_positions`: broker-authoritative position identity

## Architect Verdict

Phase 5L-E is a design-and-contract slice. It is still shippable in small steps,
but the shape is now semantic. If a remaining query still says
`trading_trades`, ask what envelope behavior it represents before touching it.
