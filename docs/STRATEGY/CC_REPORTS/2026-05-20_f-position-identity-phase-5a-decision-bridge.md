# CC_REPORT: f-position-identity-phase-5a-decision-bridge

Date: 2026-05-20

## Summary

Phase 5A shipped as an additive decision/envelope bridge. I did **not**
rename `trading_trades`; that remains a later Phase 5B/5C move after soak.

What landed:

- Mig 256: create `trading_decisions`, add nullable `trading_trades.decision_id`
  and `trading_trades.position_id`, backfill valid historical Trade rows, and
  create `trading_phase5a_envelope_parity`.
- Mig 257: install an `AFTER INSERT` trigger on `trading_trades` so future
  Trade rows automatically get a matching immutable decision row.
- Mig 258: residual backfill for rows created between mig 256 and mig 257
  during the live deployment window.
- ORM: `TradingDecision` plus `Trade.decision_id` and `Trade.position_id`.
- Tests: Phase 5A static/import tests plus existing Phase 2/3/4 canaries.

## Live verification

Schema tip:

| migration | applied |
| --- | --- |
| 258_position_identity_phase5a_residual_backfill | 2026-05-20 20:10:52 UTC |
| 257_position_identity_phase5a_trade_insert_trigger | 2026-05-20 20:09:24 UTC |
| 256_position_identity_phase5a_decision_bridge | 2026-05-20 20:07:22 UTC |

Parity view after deploy:

| metric | value |
| --- | ---: |
| trade_rows | 669 |
| trades_with_decision | 602 |
| trades_missing_decision | 67 |
| broker_trades_with_position | 500 |
| broker_trades_missing_position | 169 |
| open_broker_trades_missing_position | 0 |
| orphan_decisions | 0 |

The 67 missing-decision rows are all corrupt legacy rows with
`entry_price <= 0` or `quantity <= 0`; they were intentionally skipped so the
newer Trade check constraints are not re-triggered by UPDATEs on dead dust
rows. Among valid rows, missing decision count is zero.

Open broker positions all have both links:

| trade_id | ticker | broker | decision_id | position_id |
| ---: | --- | --- | ---: | ---: |
| 2078 | THQ-USD | coinbase | 603 | 255 |
| 2071 | ACN | robinhood | 525 | 133 |
| 2067 | ZS | robinhood | 536 | 253 |
| 2065 | ACMR | robinhood | 529 | 168 |
| 2062 | ACAD | robinhood | 520 | 46 |
| 2038 | AAVE-USD | robinhood | 563 | 19 |

Rollbacked insert probe:

- Inserted `PHASE5A-TEST` inside a transaction.
- Trigger created `trading_decisions` row and set `decision_id`.
- Transaction rolled back cleanly; no fake trade remained.

## Tests

```text
python -m pytest tests/test_position_identity_phase2.py \
  tests/test_position_identity_phase3.py \
  tests/test_position_identity_phase4.py \
  tests/test_position_identity_phase5a.py -q

34 passed in 1.31s
```

Migration verifier:

```text
OK: 258 migrations, 0 retired; no ID collisions.
```

## Data-science read

This is the right intermediate state. We now have a clean immutable decision
sample keyed by `source_trade_id`, while preserving the existing envelope table
for all live code paths. That gives us a stable unit for questions like:

- Which entry decisions produce durable position PnL across envelope rebinds?
- Which broker/venue paths degrade a decision after entry?
- Which scan patterns are good signals but bad management envelopes?

The destructive rename would not add alpha today. The decision bridge does.

## Next

Run Phase 5A soak, not the rename:

1. Daily parity probe for `valid_trades_missing_decision`,
   `open_broker_trades_missing_position`, and `orphan_decisions`.
2. Watch fresh trades to confirm the trigger keeps `decision_id` populated.
3. Only then move to Phase 5B: app-layer dual-write/read helpers around
   `TradingDecision`.
4. Rename `trading_trades -> trading_management_envelopes` remains later and
   should wait until parity is boring for at least a few days.

