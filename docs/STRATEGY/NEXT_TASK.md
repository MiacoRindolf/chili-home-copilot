# NEXT_TASK: f-position-identity-phase-5a-soak

STATUS: PENDING

## Goal

Soak the additive Phase 5A decision/envelope bridge before any destructive
rename. The system now has:

- `trading_decisions` as an immutable decision layer.
- `trading_trades.decision_id` and `trading_trades.position_id` as nullable
  management-envelope links.
- An insert trigger that creates a decision row for every new Trade row.
- `trading_phase5a_envelope_parity` as the daily health view.

## Why this is next

The rename (`trading_trades -> trading_management_envelopes`) is not the alpha
move yet. The alpha move is making decision attribution stable enough to ask
better questions: which decisions make money, which envelopes leak money, and
which broker path degrades an otherwise-good signal.

Phase 5A gives us that without risking live code paths.

## Daily probe

```sql
SELECT * FROM trading_phase5a_envelope_parity;

SELECT COUNT(*) AS valid_trades_missing_decision
FROM trading_trades
WHERE entry_price > 0
  AND quantity > 0
  AND decision_id IS NULL;

SELECT id, ticker, broker_source, status, decision_id, position_id, entry_date
FROM trading_trades
WHERE status='open'
ORDER BY id DESC;
```

Green state:

- `valid_trades_missing_decision = 0`
- `open_broker_trades_missing_position = 0`
- `orphan_decisions = 0`
- Fresh trades get `decision_id` within the same insert transaction.

Known acceptable exception:

- The 67 missing-decision rows are corrupt legacy dust rows with
  `entry_price <= 0` or `quantity <= 0`. They are intentionally skipped and
  should not be updated unless doing a separate cleanup migration.

## Phase 5B criteria

Start Phase 5B only after the parity view is boring for several days:

1. Add app-layer helper APIs around `TradingDecision`.
2. Use the decision layer for read-only reporting/comparison first.
3. Keep `trading_trades` as the live table name.
4. Delay the actual rename until helper reads and parity probes agree.

## Rollback

The Phase 5A bridge is additive. If a production problem appears:

```sql
DROP TRIGGER IF EXISTS trg_trading_trades_phase5a_after_insert ON trading_trades;
DROP FUNCTION IF EXISTS trading_trades_phase5a_after_insert();
ALTER TABLE trading_trades DROP COLUMN IF EXISTS decision_id;
ALTER TABLE trading_trades DROP COLUMN IF EXISTS position_id;
DROP VIEW IF EXISTS trading_phase5a_envelope_parity;
DROP TABLE IF EXISTS trading_decisions;
```

Prefer disabling the trigger first and leaving the backfilled data intact while
investigating.

## Reference

- CC report: `docs/STRATEGY/CC_REPORTS/2026-05-20_f-position-identity-phase-5a-decision-bridge.md`
- Migrations: 256, 257, 258
- Test suite: `tests/test_position_identity_phase5a.py`

