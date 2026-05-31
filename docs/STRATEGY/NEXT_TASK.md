# NEXT_TASK: f-position-identity-phase-5v-trades-route-shadow-compare

STATUS: PENDING

## Goal

Add a passive shadow comparison for `/api/trading/trades` so the live route keeps returning the current compatibility/ORM output while a management-envelope row builder computes the equivalent base rows in the background. Log/count mismatches; do not cut over yet.

## Recommended Work Shape

1. Keep the public `/trades` response unchanged.
2. Build a small row-normalization function shared by the current route and shadow builder where safe.
3. Compare only stable database-backed fields first; keep broker-truth overlay fields explicitly out of scope or separately compared.
4. Emit lightweight observability on mismatch count and first few mismatching trade ids.
5. Add tests proving the shadow compare is no-op on response shape and flags intentional mismatches.

## Guardrails

- No public rename.
- No route cutover unless shadow compare has run cleanly.
- No broker/order/close/reconcile/PDT/capital-gate behavior changes.
- Do not alter stale suppression semantics.

## Architect Verdict

Phase 5U proved table/view parity. Phase 5V should prove route-shape parity under the actual endpoint code before any public reader is moved.