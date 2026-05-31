# NEXT_TASK: f-position-identity-phase-5u-trades-api-parity-gate

STATUS: PENDING

## Goal

Before any public rename, build a parity gate for the user-facing `/api/trading/trades` read surface. Compare the current compatibility/ORM output with the management-envelope read model and prove they match for the fields the UI and operators rely on.

## Recommended Work Shape

1. Add a read-only helper that can materialize the `/trades` list shape from `trading_management_envelopes` without changing the route.
2. Add a probe/test that compares current `/trades` output against the helper for open and recently closed rows.
3. Keep all public names stable: `/trades`, `trade_id`, schema classes, UI copy, CSV labels, and JSON field names.
4. Do not flip the route yet unless parity is perfect and the diff is explicitly reviewed.

## Guardrails

- No broker/order/close/reconcile/PDT/capital-gate behavior changes.
- No compatibility-view drop or public rename.
- Stop if broker-display fields diverge from current route behavior.

## Architect Verdict

This is the safe bridge between private reader cleanup and any future public rename. Prove the user-facing route can be served from management envelopes before touching names.