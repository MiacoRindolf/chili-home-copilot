# NEXT_TASK: f-position-identity-phase-5k-b-coinbase-cap-reader-flag

STATUS: PENDING

## Goal

Cut over exactly one low-risk live reader behind a default-OFF feature flag:
the Coinbase venue-cap open-position aggregate in
`app/services/trading/cost_aware_gate.py`.

This is the first Phase 5K live-path cutover because Phase 5K-A proved the
`trading_trades` compatibility view and `trading_management_envelopes` base
table produce identical Coinbase-cap aggregate inputs.

## Scope

1. Add a setting, default `False`, for example:

   ```text
   CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false
   ```

2. In `per_venue_cap_check`, choose the source relation:

   - flag OFF: `trading_trades` compatibility view
   - flag ON: `trading_management_envelopes` base table

3. Keep all filters and behavior identical.

4. Add tests for:

   - default flag uses `trading_trades`
   - flag ON uses `trading_management_envelopes`
   - failure behavior remains conservative

5. Run:

   ```powershell
   python -m pytest tests\test_phase5k_live_path_parity_probe.py tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5k-live-path-parity-probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   ```

## Guardrails

- Do not flip the flag in `.env`.
- Do not restart services.
- Do not edit broker/order/stop/reconcile paths.
- Do not edit dirty local files.
- No live behavior change in this slice.

## Acceptance

- Tests pass.
- Phase 5K-A parity probe remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.
- Feature flag default keeps current behavior.
