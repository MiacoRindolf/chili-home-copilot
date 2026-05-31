# Phase 5O AutoTrader Desk Envelope Audit

Date: 2026-05-31

## Verdict

`app/services/trading/autotrader_desk.py` is an operator-visible live position
surface, not a private helper adapter candidate.

The surface already loads live rows through
`load_autotrader_desk_live_envelope_objects(...)`, but its remaining legacy
`Trade` symbol contract is still behavior-bearing: the desk suppresses
broker-stale rows, applies extra position-identity suppression, overlays
broker-truth metrics, routes broker/market quotes, fetches per-position
overrides, classifies monitor scope, detects option/crypto rows, and exposes
close/control affordances.

No desk behavior was converted in this slice.

## Behavior Boundary

Audited legacy `Trade` usage in these surfaces:

- `_broker_quote_price_for_trade(...)` uses broker source, ticker, and option
  state to fetch venue-correct display quotes.
- `_trade_asset_type(...)` classifies operator-facing stock/crypto/options
  labels from the live row.
- `list_pattern_linked_open_positions(...)` loads open live management
  envelopes, applies broker-stale suppression, fetches paper rows, enriches
  live rows with pattern names, overrides, broker truth, quote data, close
  support, and unrealized PnL.

The desk is display-first, but it is not harmless naming: it is the UI's view of
whether a live position exists and whether the operator may pause/exclude/close
it.

## Evidence Added

Added read-only probe:

```text
scripts/d-phase5o-autotrader-desk-envelope-parity-probe.py
```

The probe does not call close/override mutations and does not call the desk
endpoint. It compares the desk's live inputs through:

- `trading_trades` compatibility view
- `trading_management_envelopes` physical table

Live result with explicit read-only probe user
`PHASE5O_AUTOTRADER_DESK_USER_ID=1`:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=5 AutoTrader desk checks matched
PROBE_USER_ID=1
AUTOTRADER_DESK_MISMATCHES=0
desk_broker_truth_inputs old=8 new=8
desk_live_rows old=8 new=8
desk_override_keys old=8 new=8
desk_quote_inputs old=8 new=8
desk_scope_by_trade_id old=8 new=8
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
```

Added focused tests:

```text
tests/test_phase5o_autotrader_desk_probe.py
```

## Verification

- `python -m py_compile scripts\d-phase5o-autotrader-desk-envelope-parity-probe.py app\services\trading\autotrader_desk.py scripts\analyze_phase5_remaining_trade_refs.py`
  passed.
- JSON map validation passed.
- Focused tests passed:
  `tests/test_phase5o_autotrader_desk_probe.py` and
  `tests/test_phase5o_remaining_runtime_compat_map.py`
  (`7 passed`, one existing SQLAlchemy sorted-table warning).
- Analyzer reported no unexpected runtime readers/mutations.
- Phase 5K live-path parity remained `COMPLETE_POSITIVE`.
- Phase 5I post-rename soak remained `COMPLETE_POSITIVE`.
- Source posture remains `ALERT` because app services are still mounted from
  dirty root `D:\dev\chili-home-copilot` by a shared/external process. This
  slice did not restart Postgres, touch `.env`, refresh runtime, mutate DB, or
  change live behavior.

## Inventory Movement

```text
adapter_candidate: 3 -> 2
future_rename_blocker: 46 -> 47
private_helper_type_only: 4 -> 3
live_action_broker_reconcile: 20 -> 21
orm_trade_symbol_compat: 65 unchanged
```

## Next Recommended Slice

Audit `app/services/trading/paper_trading.py`. It is one of the final two
adapter candidates. Although it is paper/research-facing, paper-trade outcomes
feed learning and live-drift comparisons, so it needs direct evidence before it
can be treated as a safe adapter target.
