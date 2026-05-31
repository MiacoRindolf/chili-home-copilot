# NEXT_TASK: f-position-identity-phase-5w-trades-route-shadow-soak-review

STATUS: PENDING

## Goal

Review Phase 5V shadow-compare evidence before any `/trades` route cutover. If no `[phase5v]` mismatch logs appear during normal UI/API use, decide whether to keep observing or implement a feature-flagged read cutover.

## Recommended Work Shape

1. Inspect recent app logs for `[phase5v] /trades envelope shadow mismatch`.
2. Exercise `/api/trading/trades`, `/api/trading/trades?status=open`, and `/api/trading/trades?status=closed` once after deployment.
3. If mismatch-free, draft a feature-flagged cutover plan; do not public-rename yet.
4. If mismatches appear, classify by broker-truth overlay, stale suppression, or true envelope divergence.

## Guardrails

- No public rename.
- No route cutover without clean shadow evidence.
- No broker/order/close/reconcile/PDT/capital-gate behavior changes.

## Architect Verdict

Soak the passive compare first. The next actual behavior change should be feature-flagged, reversible, and limited to the read route.