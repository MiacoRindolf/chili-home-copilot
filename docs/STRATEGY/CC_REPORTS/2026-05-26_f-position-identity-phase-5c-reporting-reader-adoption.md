# f-position-identity-phase-5c-reporting-reader-adoption

Date: 2026-05-26
Status: SHIPPED
Branch: `codex/stock-activity-fractional-gate`

## Executive Summary

Phase 5C shipped the first reporting-reader adoption of the Phase 5B
decision/envelope/position read model. The live-vs-research attribution
endpoint keeps its legacy response by default, and now exposes an opt-in
comparison with:

```text
/api/trading/attribution/live-vs-research?phase5b_compare=true
```

No trading writer, close path, broker reconciler, or autotrader behavior was
changed.

## What Changed

- Mig 274 appends `decision_scan_pattern_id` and
  `envelope_scan_pattern_id` to `trading_phase5b_decision_envelope_position`.
- Mig 274 appends `pattern_attribution_mismatches` to
  `trading_phase5b_pattern_decision_performance`.
- `live_vs_research_by_pattern(..., include_phase5b_compare=True)` now returns
  `phase5b_compare`.
- `/api/trading/attribution/live-vs-research` accepts
  `phase5b_compare=false` by default, preserving old behavior unless the
  caller asks for the read-model comparison.

## Live Verification

Migration tip:

```text
274_position_identity_phase5c_attribution_columns
```

View columns present:

```text
scan_pattern_id
decision_scan_pattern_id
envelope_scan_pattern_id
```

30-day live compare:

```text
closed_rows: 320
mismatched_rows: 4
mismatched_pnl: $21.2641
```

Service compare summary:

```text
envelope_pattern_groups: 25
decision_pattern_groups: 25
envelope_closed_envelopes: 320
decision_closed_envelopes: 320
mismatched_pattern_groups: 3
mismatched_closed_envelopes: 4
absolute_group_pnl_delta: $42.5282
null_decision_pattern_envelopes: 4
```

Mismatch detail:

```text
decision=None, envelope=1072, closed=2, pnl=$19.8119
decision=None, envelope=1248, closed=1, pnl=$0.9690
decision=None, envelope=1250, closed=1, pnl=$0.4832
```

## Architect/Data-Science Read

The rename gate is healthier, but not ready for the destructive physical table
rename yet.

The important signal is that hard linkage is clean and the remaining
difference is semantic attribution: historical bridge-created decisions missed
the pattern id while their management envelopes retained it. That means the
position model is structurally sound, and the remaining work is to decide how
to repair legacy attribution without hiding the provenance of that repair.

Recommended next move: Phase 5D backfills only `trading_decisions.scan_pattern_id
IS NULL` where the linked envelope has a non-null `scan_pattern_id`, records the
repair reason, reruns the Phase 5C compare, then soaks the reporting endpoint
through multiple fresh closes.

## Verification

```text
python -m py_compile app\migrations.py app\services\trading\attribution_service.py app\routers\trading_sub\trades.py tests\test_position_identity_phase5c.py
python -m pytest tests\test_position_identity_phase5b.py tests\test_position_identity_phase5c.py tests\test_trading.py::TestAttributionAPI::test_live_vs_research_endpoint -q
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify-migration-ids.ps1
```

Result:

```text
10 passed
OK: 274 migrations, 0 retired; no ID collisions.
```

## Rollback

No data-writer rollback is required. Disable callers from passing
`phase5b_compare=true`, or revert the reporting commit. The Phase 5B/5C views
are read-only and can remain installed.
