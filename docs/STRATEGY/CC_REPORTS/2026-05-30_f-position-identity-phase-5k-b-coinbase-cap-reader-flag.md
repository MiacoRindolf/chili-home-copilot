# f-position-identity-phase-5k-b-coinbase-cap-reader-flag

## Summary

Phase 5K-B shipped the first live-path cutover hook: a default-OFF feature flag
for the Coinbase venue-cap reader in `cost_aware_gate.py`.

No live behavior changes by default:

- flag OFF: read from `trading_trades` compatibility view
- flag ON: read from `trading_management_envelopes` semantic base table

The flag was not flipped in `.env`, and no service was restarted.

## What Landed

- `app/services/trading/cost_aware_gate.py`
  - adds `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES`
  - defaults to compatibility view
  - chooses only one of two hard-coded relation names
  - keeps all filters and cap behavior identical
  - preserves conservative failure behavior
- `tests/test_cost_aware_gate.py`
  - default flag uses `trading_trades`
  - flag ON uses `trading_management_envelopes`
  - query failure still blocks conservatively

The global `app/config.py` file already had unrelated local edits, so this
slice deliberately avoided touching it. The gate reads the passed settings
object first and falls back to the environment variable.

## Verification

Commands run:

```powershell
python -m py_compile app\services\trading\cost_aware_gate.py tests\test_cost_aware_gate.py
python -m pytest tests\test_cost_aware_gate.py::test_cap_phase5k_flag_defaults_to_compatibility_view tests\test_cost_aware_gate.py::test_cap_phase5k_flag_can_use_management_envelopes tests\test_cost_aware_gate.py::test_cap_phase5k_query_failure_stays_conservative tests\test_phase5k_live_path_parity_probe.py tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py -q
python scripts\d-phase5k-live-path-parity-probe.py
python scripts\d-phase5i-post-rename-soak-probe.py
```

Results:

- focused tests: 13 passed
- Phase 5K-A parity probe: `COMPLETE_POSITIVE`
- Phase 5I direct probe: `COMPLETE_POSITIVE`

The wider DB-backed `tests/test_cost_aware_gate.py` file was attempted but hit a
test-database truncate deadlock in the shared `db` fixture before the selected
test body ran:

```text
psycopg2.errors.DeadlockDetected
```

That failure is not specific to this change. The new Phase 5K-B tests avoid the
DB fixture and passed cleanly.

## Architect Read

This is the right shape for the first live-path hook: tiny, default-off,
hard-coded relation choice, no dynamic SQL input, and covered by a parity probe.

Next safe move is a short flag-flip soak for this one reader only:

1. Confirm Phase 5K-A parity is still `COMPLETE_POSITIVE`.
2. Set `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true`.
3. Restart only the autotrader worker.
4. Watch for cap decisions and parity probe mismatches.

Because the old and new relation currently match exactly, this should be
behavior-neutral. Still, it is a live gate, so the flip should be logged as a
separate soak step.
