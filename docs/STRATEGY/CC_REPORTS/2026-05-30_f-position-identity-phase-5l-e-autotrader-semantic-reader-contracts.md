# Phase 5L-E: Autotrader Semantic Reader Contracts

**Date:** 2026-05-30
**Status:** SHIPPED
**Commits:** `acd39d5`, `f7f3f81`, `4e45841`, merge `409b92d`

## Summary

Moved the autotrader live-reader surfaces that still had raw
`trading_trades` SQL behind named management-envelope helper APIs.

Converted semantic readers:

- AutoTrader open-by-lane exposure count.
- Synergy retry candidate lookup.
- Probation recert daily quota count.

No order-placement, broker-sync, or reconcile behavior changed. The code still
counts active management envelopes with the same filters and same conservative
failure behavior; it just no longer has raw autotrader reader SQL pointed at the
legacy compatibility view.

## Code Changes

- Added helper APIs in `app/services/trading/management_envelopes.py`:
  - `count_open_autotrader_envelopes_by_lane(...)`
  - `fetch_synergy_retry_envelope_candidates(...)`
  - `count_probation_envelopes_since(...)`
- Routed `auto_trader_rules.count_autotrader_v1_open_by_lane` through the open
  lane helper.
- Routed `auto_trader._synergy_retry_candidates` through the retry helper.
- Routed `auto_trader._probation_trade_count_today` through the probation helper.
- Reduced `tests/test_phase5l_reader_allowlist.py` to the remaining exact raw
  reader lines: bracket reconciliation and Coinbase orphan adoption.

The commit was intentionally scoped. A pre-existing unrelated local
`auto_trader.py` direction-line edit remains out of the committed Phase 5L-E
tree.

## Verification

Focused tests:

```text
python -m pytest tests\test_auto_trader_rules.py::test_count_autotrader_v1_open_by_lane_counts_working_and_asset_kind tests\test_phase5l_reader_allowlist.py -q
2 passed

python -m pytest tests\test_auto_trader_safety.py::test_synergy_retry_candidates_revisit_recent_distinct_pattern -q
1 passed

python -m pytest tests\test_auto_trader_safety.py::test_probation_recert_live_entry_reduces_size_and_enforces_daily_quota -q
1 passed
```

The first combined DB-backed run hit the shared pytest DB advisory lock, and a
second combined run surfaced a transient FK fixture race. Both affected tests
passed individually afterward.

Compile/probes:

```text
python -m py_compile app\services\trading\auto_trader.py app\services\trading\auto_trader_rules.py app\services\trading\management_envelopes.py tests\test_phase5l_reader_allowlist.py

python scripts\d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

python scripts\d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

Live smoke:

```text
count_open_autotrader_envelopes_by_lane(user_id=1, autotrader_version='v1')
-> {'equity': 1, 'crypto': 4, 'options': 0}
```

Runtime:

- Recreated `autotrader-worker`.
- Worker came back up cleanly.
- Short log soak showed no relation/schema/runtime errors for the touched paths.

## Architect Note

This was the right Phase 5L shape: not "rename a table," but identify the
business question and give it a named envelope API. The autotrader code now
speaks in management-envelope terms where it is counting exposure, retrying
alerts against an existing envelope, or enforcing probation quota.

## Next

The Phase 5L canary now only allows:

- `bracket_reconciliation_service.py`: two `FROM trading_trades AS t` lines.
- `venue/coinbase_orphan_adopt.py`: one `JOIN trading_trades t ...` line.

Next slice should wrap those as bracket/orphan semantic readers. Do not drop the
compatibility view and do not rename the `Trade` ORM class.
