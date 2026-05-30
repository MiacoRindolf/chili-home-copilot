# f-position-identity-phase-5k-a-live-path-parity-probe

## Summary

Phase 5K-A shipped a read-only parity probe for the live decision surfaces that
still intentionally read through the `trading_trades` compatibility view.

The probe compares each aggregate through:

- `trading_trades` — compatibility view
- `trading_management_envelopes` — semantic physical base table

No live trading behavior changed.

## What Landed

- `scripts/d-phase5k-live-path-parity-probe.py`
  - read-only probe
  - emits `COMPLETE_POSITIVE`, `REGRESSION_SCHEMA`, `REGRESSION_PARITY`, or
    `ALERT`
  - checks relation kinds before comparing data
  - prints per-check row counts and mismatch details if parity breaks
- `tests/test_phase5k_live_path_parity_probe.py`
  - pins that the probe intentionally references both relation names
  - blocks DDL/DML keywords in the probe source
  - verifies mismatch detection

## Live Checks

The probe compares:

- Coinbase venue cap:
  - open auto-trader Coinbase count
  - open auto-trader Coinbase notional
- PDT guard:
  - true 5-business-day equity day-trade count
- promotion/cohort realized aggregate:
  - 90d closed PnL and average return by `scan_pattern_id`
- pattern-quality realized aggregate:
  - 90d winners, losers, trades, and PnL by `scan_pattern_id`
- portfolio-risk open exposure:
  - open count and notional by broker/source and asset kind
- position-integrity linkage:
  - open envelope counts
  - missing `position_id`
  - missing position row
  - current-envelope mismatch

## Verification

Commands run:

```powershell
python -m py_compile scripts\d-phase5k-live-path-parity-probe.py tests\test_phase5k_live_path_parity_probe.py
python -m pytest tests\test_phase5k_live_path_parity_probe.py tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5k-live-path-parity-probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Results:

- 10 tests passed
- Phase 5K-A probe: `COMPLETE_POSITIVE`
- Phase 5I direct probe: `COMPLETE_POSITIVE`
- Phase 5I scheduled wrapper: `COMPLETE_POSITIVE`
- `LOG_SCHEMA_ERRORS=0`

Latest Phase 5K-A probe output:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=6 live-path aggregate checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_COINBASE_CAP=OK old_rows=1 new_rows=1
CHECK_PDT_DAY_TRADES=OK old_rows=1 new_rows=1
CHECK_PROMOTION_REALIZED=OK old_rows=30 new_rows=30
CHECK_PATTERN_QUALITY=OK old_rows=30 new_rows=30
CHECK_PORTFOLIO_RISK_OPEN=OK old_rows=2 new_rows=2
CHECK_POSITION_INTEGRITY_OPEN=OK old_rows=1 new_rows=1
```

## Architect Read

This is the evidence layer we needed before touching live paths. The semantic
base table and compatibility view currently produce identical inputs for the
main live decision surfaces.

Next safe move: Phase 5K-B should cut over exactly one low-risk live reader
behind a feature flag and keep the parity probe as the rollback/evidence guard.
The best candidate is the Coinbase venue-cap reader in
`cost_aware_gate.py`: it is a small read-only SELECT, it directly benefits from
semantic naming, and the new probe already covers its aggregate.

Do not start with broker sync, bracket reconciliation, exit monitors, or
position repair. Those are writer boundaries, not reader cleanup.
