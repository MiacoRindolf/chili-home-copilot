# f-position-identity-phase-5i-post-rename-soak-closeout

## Summary

Phase 5I is green after a narrow attribution repair.

The physical rename remains healthy:

- `trading_management_envelopes` is the physical base table (`relkind='r'`)
- `trading_trades` is the legacy compatibility view (`relkind='v'`)
- `trading_phase5b_decision_envelope_position` is present as a view (`relkind='v'`)

The post-rename soak now has organic data and no blocking drift:

- Fresh decisions after mig 283: 20
- Fresh envelopes after mig 283: 20
- Fresh closed envelopes after mig 283: 10
- Fresh close mismatches: 0
- Hard linkage issues: 0
- 30d closed attribution mismatches: 0
- 30d mismatched PnL: $0.0000
- Schema-specific worker log hits: 0

## Repair

The watcher briefly reported `BLOCKED_DRIFT` because four fresh closed Coinbase
envelopes had `decision_scan_pattern_id=NULL` while their linked management
envelope had a valid `scan_pattern_id`.

Root cause: the Phase 5A decision row is created at envelope insert time, while
some Coinbase / sync-close paths finalize `scan_pattern_id` later.

Migration `288_position_identity_phase5i_pattern_sync` fixes this by:

- Backfilling only missing decision attribution from the linked envelope.
- Installing an `AFTER UPDATE OF scan_pattern_id` trigger on the active envelope
  base relation.
- Preserving any non-NULL decision/envelope disagreement as diagnostic instead
  of overwriting it.

The scheduled watcher wrapper was also tightened so Postgres restart noise does
not count as a rename-path schema error.

## Verification

Commands run:

```powershell
python -m py_compile app\migrations.py scripts\d-phase5i-post-rename-soak-probe.py
python -m pytest tests\test_phase5i_post_rename_probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
```

Final watcher verdict:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=fresh post-rename data clean: decisions=20, envelopes=20, closes=10
FRESH_CLOSE_MISMATCHES=0
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
MISMATCHED_PNL=0.0000
LOG_SCHEMA_ERRORS=0
EXIT_CODE=0
```

## Architect Read

The rename has soaked enough to proceed with selective reader cleanup. Do not
drop the `trading_trades` compatibility view yet, and do not rename the Python
`Trade` ORM class yet. The next move should be incremental Phase 5J: prefer
`trading_management_envelopes` in new analytics/reporting SQL while keeping live
writer paths boring.
