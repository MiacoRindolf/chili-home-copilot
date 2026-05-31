# Phase 5L-J - Private Helper ORM Surface

Date: 2026-05-31

## Summary

Phase 5L-J reduces the low-risk `private_helper_type_only` legacy `Trade` ORM
symbol surface without touching live broker/order/close/reconcile behavior,
schema, public API fields, UI labels, or database relation names.

This was intentionally a small wording cleanup. The remaining private-helper
entries are real ORM consumers or compatibility exports, not stale comments.

## Result

The `private_helper_type_only` group dropped from 10 files to 7 files:

```text
private_helper_type_only | 10 -> 7
orm_trade_symbol_compat  | 96 -> 93
```

Removed from the compatibility surface:

```text
app/services/trading/management_envelopes.py
app/services/trading/venue/rate_limiter.py
app/services/trading/venue/robinhood_options.py
```

These were comment/docstring-only hits:

- `management_envelopes.py` described envelope-shaped readers as
  `Trade-like`.
- `rate_limiter.py` used Coinbase's product phrase `Advanced Trade`, which
  was a false-positive for the legacy ORM symbol scanner.
- `robinhood_options.py` described broker matching against `Trade rows` rather
  than local position envelopes.

## Architect verdict

The remaining 7 private-helper files should not be mechanically changed in this
slice. They include the model/export compatibility surface and helper modules
that genuinely accept, query, or close live legacy ORM objects. Further
reduction should happen only through a deliberate runtime adapter or deployment
posture task, not a text rename.

