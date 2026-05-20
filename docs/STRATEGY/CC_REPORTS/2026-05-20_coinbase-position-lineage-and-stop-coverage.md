# Coinbase Position Lineage + Stop Coverage, 2026-05-20

## Why

The remaining "observation only" items turned into a live Coinbase integrity
issue. THQ-USD was repeatedly auto-closed and re-created by Coinbase position
sync while Coinbase still had working stop-limit sell orders. The Trade
envelope existed, but the position-identity sidecar and execution-event lineage
were incomplete, and the bracket reconciler could not see Coinbase stop orders.

## Shipped

- Coinbase broker sync now seeds `trading_positions`, updates
  `current_envelope_id`, stamps bracket-intent `position_id`, and writes one
  idempotent synthetic `coinbase_position_sync_entry` event for auto-synced
  broker inventory.
- `resolve_position_id()` now supports `user_id IS NULL` via a COALESCE natural
  key match. The previous truthiness check made NULL-user broker-sync positions
  unresolvable.
- Coinbase sync now canonicalizes NULL-user manual syncs to the most recent
  non-NULL Coinbase owner id, preventing the same exchange position from
  splitting into user_id NULL and user_id 1 envelopes.
- Coinbase stale-close now refuses to close a ticker that disappears from
  `get_positions()` while Coinbase still reports working sell orders for that
  ticker.
- Bracket reconciliation now reads Coinbase open stop orders and surfaces them
  in `BrokerView`.
- Bracket writer now sums existing Coinbase stop-limit sell coverage and places
  only the uncovered remainder, preventing over-cover retry storms.

## Live Verification

- THQ-USD open envelope stabilized at a single open trade after dual sync calls
  (`sync_positions_to_db(... user_id=None)` and `user_id=1`): open count stayed
  at 1 and max trade id stayed at 2077 on the second pass.
- THQ-USD position identity is linked: `trading_positions.current_envelope_id`
  points to trade 2077, and the synthetic entry event has non-NULL
  `position_id`.
- Coinbase had four working THQ-USD stop-limit sells totaling exactly 29104.0
  base units, matching the broker position quantity.
- The reconciler placed the uncovered THQ-USD remainder once
  (`abdc358c...`, qty 10595.5) and then moved the current bracket intent to
  reconciled/adopted-broker-tighter-stop.

## Tests

Fast guard suite passed:

```text
pytest -q tests/test_coinbase_position_lineage.py \
          tests/test_coinbase_split_stop_coverage.py \
          tests/test_coinbase_dust_auto_create_skip.py \
          tests/test_position_identity_phase2.py -q
24 passed
```

The older DB-heavy bracket suites timed out in this desktop shell before
assertions; live container import/restart and direct DB verification were used
for the production checks.

