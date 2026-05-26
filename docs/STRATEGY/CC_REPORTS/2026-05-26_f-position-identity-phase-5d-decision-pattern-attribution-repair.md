# f-position-identity-phase-5d-decision-pattern-attribution-repair

Date: 2026-05-26
Status: SHIPPED
Branch: `codex/stock-activity-fractional-gate`

## Executive Summary

Phase 5D repaired the small semantic pattern-attribution drift found by Phase
5C. The repair was intentionally narrow: only `trading_decisions.scan_pattern_id
IS NULL` rows were backfilled, and only when their linked management envelope
had a non-null `scan_pattern_id`.

No live trading writer, broker reconciler, close path, or physical table rename
changed.

## What Changed

- Mig 275:
  - joins `trading_decisions.source_trade_id` to `trading_trades.id`
  - fills `trading_decisions.scan_pattern_id` from
    `trading_trades.scan_pattern_id`
  - only runs where the decision pattern is NULL and envelope pattern is
    non-null
  - appends a provenance marker to `trading_decisions.notes`
- Tests pin the non-overwrite predicate and migration ordering.

Provenance marker:

```text
phase5d_pattern_backfill_from_envelope source_trade_id=<id> scan_pattern_id=<id>
```

## Live Verification

Migration tip:

```text
275_position_identity_phase5d_decision_pattern_backfill
```

Repaired decisions:

```text
decision_id=604 source_trade_id=2080 scan_pattern_id=1072
decision_id=613 source_trade_id=2089 scan_pattern_id=1072
decision_id=623 source_trade_id=2099 scan_pattern_id=1248
decision_id=624 source_trade_id=2100 scan_pattern_id=1250
decision_id=625 source_trade_id=2101 scan_pattern_id=1248
```

30-day Phase 5C compare after repair:

```text
closed_rows: 320
mismatched_rows: 0
mismatched_pnl: $0.0000
```

Service compare summary:

```text
envelope_pattern_groups: 25
decision_pattern_groups: 25
envelope_closed_envelopes: 320
decision_closed_envelopes: 320
mismatched_pattern_groups: 0
mismatched_closed_envelopes: 0
absolute_group_pnl_delta: $0.0000
null_decision_pattern_envelopes: 0
```

## Architect/Data-Science Read

This closes the known reporting-attribution wrinkle from Phase 5C. The rename
gate is now materially stronger: hard linkage was already green after mig 273,
and semantic pattern attribution is now green after mig 275.

The right next step is not the destructive physical rename yet. The right next
step is a short Phase 5E reporting-reader soak: let fresh entries and closes
arrive, rerun the Phase 5C compare, and require zero unexplained drift before
the table rename.

## Verification

```text
python -m py_compile app\migrations.py tests\test_position_identity_phase5d.py
python -m pytest tests\test_position_identity_phase5c.py tests\test_position_identity_phase5d.py -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-migration-ids.ps1
```

Result:

```text
6 passed
OK: 275 migrations, 0 retired; no ID collisions.
```

## Rollback

If a rollback is ever needed, reset only the repaired decision IDs listed
above, and remove the Phase 5D provenance line from `notes`. No live trading
writer state depends on this repair.
