# f-position-identity-phase-5k-g-position-integrity-reader-flag

Date: 2026-05-30

Status: PROMOTED. Live flag is ON.

## What Changed

Added a default-off Phase 5K reader flag to `app/services/trading/position_integrity.py`:

- `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false` (default)
- OFF reads `trading_trades` compatibility view
- ON reads `trading_management_envelopes` physical base table

The switched reader surface:

- `audit_position_identity`
- `repair_current_envelope_links`

The live working tree also contains an uncommitted orphan-sidecar helper in this
file. The runtime implementation uses the same relation helper there so the
soak is honest, but the commit deliberately does not absorb that unrelated
sidecar feature.

No integrity verdict semantics, repair predicates, broker paths, order paths,
stop paths, or reconcile write paths changed.

## Default-Off Verification

Focused tests:

```text
python -m pytest tests\test_phase5k_position_integrity_reader_flag.py tests\test_phase5k_live_path_parity_probe.py -q
13 passed
```

Compile check:

```text
python -m py_compile app\services\trading\position_integrity.py scripts\d-phase5k-live-path-parity-probe.py
OK
```

Live Phase 5K-A parity:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
CHECK_POSITION_INTEGRITY_OPEN=OK old_rows=5 new_rows=5
```

Direct old/new checks:

```text
AUDIT_COUNTS_OLD {'open_positions_without_open_trade': 0, 'open_trades_without_open_position': 0, 'open_positions_missing_current_envelope': 0, 'current_envelope_mismatches': 0, 'repairable_current_envelope_links': 0}
AUDIT_COUNTS_NEW {'open_positions_without_open_trade': 0, 'open_trades_without_open_position': 0, 'open_positions_missing_current_envelope': 0, 'current_envelope_mismatches': 0, 'repairable_current_envelope_links': 0}
AUDIT_MATCH True
REPAIR_OLD {'eligible': 0, 'stale': 0, 'updated': 0, 'cleared': 0}
REPAIR_NEW {'eligible': 0, 'stale': 0, 'updated': 0, 'cleared': 0}
REPAIR_MATCH True
```

## Rollback

Leave or set `CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=false`, then
recreate the consumer worker(s).

## Live Soak

Flag flipped:

```text
CHILI_PHASE5K_POSITION_INTEGRITY_USE_ENVELOPES=true
```

Consumers recreated:

- `chili`
- `broker-sync-worker`
- `autotrader-worker`

Runtime flag visibility:

```text
chili=true
broker-sync-worker=true
autotrader-worker=true
```

Post-flip verification:

```text
Phase 5K-A: COMPLETE_POSITIVE, PARITY_MISMATCHES=0
Phase 5I: COMPLETE_POSITIVE, HARD_LINKAGE_ISSUES=0, MISMATCHED_ROWS=0
AUDIT_MATCH=True
REPAIR_MATCH=True
```

Post-flip log evidence:

```text
position-integrity/relation/query errors: none
```

The live sidecar cleanup path also ran after the flip and reported
`position sidecars closed=0`, with no relation or query errors.
