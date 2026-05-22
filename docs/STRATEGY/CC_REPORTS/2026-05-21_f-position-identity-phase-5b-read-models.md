# CC_REPORT: f-position-identity-phase-5b-read-models

Date: 2026-05-21

## Summary

Phase 5B shipped the semantic envelope layer without physically renaming
`trading_trades`.

What landed:

- Mig 264: `trading_management_envelopes` compatibility view over
  `trading_trades`.
- Mig 264: `trading_phase5b_decision_envelope_position`, a joined read model
  for `trading_decisions -> envelope -> trading_positions`.
- Mig 264: `trading_phase5b_pattern_decision_performance`, a read-only
  pattern-level decision/envelope performance view.
- Mig 265: linkage-status refinement that separates hard live linkage issues
  from closed historical broker-envelope debt.
- `app/services/trading/management_envelopes.py`: read-only helper API for
  Phase 5B parity, envelope fetches, and pattern decision performance.
- `tests/test_position_identity_phase5b.py`: static and mocked helper tests.

No live trading behavior changed. No physical table rename happened.

## Why This Shape

The rename is still high-blast-radius. Phase 5B gives code and reporting a
semantic surface that says "management envelope" while the old table name stays
alive underneath. This lets us migrate readers and data-science queries first,
then rename later once the old name is no longer semantically load-bearing.

## Verification

Fast tests:

```text
python -m pytest tests/test_position_identity_phase5a.py \
  tests/test_position_identity_phase5b.py -q

11 passed
```

Migration verifier:

```text
OK: 265 migrations, 0 retired; no ID collisions.
```

Live DB verification should confirm:

```sql
SELECT to_regclass('public.trading_management_envelopes');
SELECT to_regclass('public.trading_phase5b_decision_envelope_position');
SELECT to_regclass('public.trading_phase5b_pattern_decision_performance');
SELECT * FROM trading_phase5a_envelope_parity;
SELECT linkage_status, COUNT(*)
FROM trading_phase5b_decision_envelope_position
GROUP BY linkage_status
ORDER BY COUNT(*) DESC;
```

Live verification on 2026-05-22 after applying migs 264-265:

- Views installed: all three `to_regclass(...)` checks returned present.
- Phase 5A parity: 679 trade rows, 612 with decisions, 67 corrupt legacy
  dust rows without decisions, 0 open broker trades missing positions, 0
  orphan decisions.
- Phase 5B linkage status: 506 `linked`, 106
  `historical_broker_envelope_missing_position`, 0 hard live linkage issues.
- Top performance rows surfaced as expected: pattern 585 +$521.11, 537
  +$82.09, 586 +$57.96, 1052 +$57.42.

## Next

Soak Phase 5B. Do not rename yet. The next useful step is reader adoption:

1. Point reporting/analysis code at `management_envelopes.phase5b_parity_summary`
   and `pattern_decision_performance`.
2. Compare old Trade-row PnL reports vs the Phase 5B decision/envelope views.
3. Only after that, ship Phase 5C to migrate specific readers.
4. The physical rename remains last.
