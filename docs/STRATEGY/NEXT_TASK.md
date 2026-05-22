# NEXT_TASK: f-position-identity-phase-5b-soak-and-reader-parity

STATUS: PENDING

## Goal

Soak the Phase 5B read-only envelope layer and compare old Trade-row reports
against the new decision/envelope/position views before migrating any live
reader.

## What Exists Now

- `trading_management_envelopes`: compatibility view over `trading_trades`.
- `trading_phase5b_decision_envelope_position`: joined read model for
  `trading_decisions -> management envelope -> trading_positions`.
- `trading_phase5b_pattern_decision_performance`: pattern-level performance
  view using the decision/envelope split.
- `app.services.trading.management_envelopes`: read-only helper API.
- Linkage status separates hard live failures from
  `historical_broker_envelope_missing_position` debt.

## Daily Probe

```sql
SELECT * FROM trading_phase5a_envelope_parity;

SELECT linkage_status, COUNT(*)
FROM trading_phase5b_decision_envelope_position
GROUP BY linkage_status
ORDER BY COUNT(*) DESC;

SELECT *
FROM trading_phase5b_pattern_decision_performance
ORDER BY total_pnl DESC NULLS LAST
LIMIT 20;
```

Green state:

- `valid_trades_missing_decision = 0`
- `open_broker_trades_missing_position = 0`
- `orphan_decisions = 0`
- Phase 5B hard linkage issues are zero.
- `historical_broker_envelope_missing_position` can remain nonzero until old
  closed envelopes are backfilled or retired from reports.

## Phase 5C Criteria

Move to Phase 5C when the read model stays boring through multiple fresh
entries and at least one close:

1. Add one reporting reader that uses `management_envelopes.py`.
2. Compare old `trading_trades` report output to Phase 5B output.
3. Keep the old query live until the comparison is stable.
4. Do not physically rename `trading_trades` yet.

## Rollback

Phase 5B is read-only. Rollback is just:

```sql
DROP VIEW IF EXISTS trading_phase5b_pattern_decision_performance;
DROP VIEW IF EXISTS trading_phase5b_decision_envelope_position;
DROP VIEW IF EXISTS trading_management_envelopes;
```

The helper module can remain unused if the views are dropped.

## Reference

- CC report: `docs/STRATEGY/CC_REPORTS/2026-05-21_f-position-identity-phase-5b-read-models.md`
- Migrations: 264-265
- Test suite: `tests/test_position_identity_phase5b.py`
