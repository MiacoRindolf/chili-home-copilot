# NEXT_TASK: f-phase5aa-market-data-anchor-parity-probe

STATUS: QUEUED

## Goal

Build a read-only old-vs-new parity probe for
`market_data._resolve_implausibility_anchor(...)` before any conversion away
from direct `Trade` ORM access.

## Why This Is Next

Phase 5Z audited the remaining candidate readers and found that
`market_data._resolve_implausibility_anchor(...)` is not passive reporting. It
uses the most recent open trade entry price as a quote-plausibility anchor, and
that can influence `fetch_quote(...)` behavior. Treat it as live market-data
safety behavior.

The right next step is therefore a probe, not a swap.

## Scope

- Add a read-only probe that compares the current `Trade` ORM anchor result to
  a candidate management-envelope anchor result.
- Cover tickers with open envelopes, missing anchors, and stale/closed rows.
- Report old anchor, new anchor, match status, relation kinds, and drift count.
- Do not flip flags or change runtime behavior.

## Guardrails

- No broker/order/close/reconcile changes.
- No stop execution/evaluation changes.
- No risk/capital/PDT/portfolio gate changes.
- No quote-routing behavior change.
- No public `/trades`, `trade_id`, schema, or UI label rename.
- If parity is not exact, document the drift and stop.

## Exit Criteria

- Probe emits `COMPLETE_POSITIVE` only when old and new anchors match exactly
  for the sampled live universe.
- Focused tests cover old/new parity and mismatch reporting.
- Phase 5K live-path parity and Phase 5I post-rename soak remain
  `COMPLETE_POSITIVE`.

