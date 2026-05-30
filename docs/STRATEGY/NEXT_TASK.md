# NEXT_TASK: f-position-identity-phase-5k-c-coinbase-cap-flag-soak

STATUS: PENDING

## Goal

Flip `CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true` for the autotrader worker
and run a short soak of the Coinbase venue-cap reader.

This is behavior-neutral if Phase 5K-A parity remains green, but it is still a
live gate. Treat as a separate flag-flip soak.

## Preconditions

- Phase 5K-A parity probe emits `COMPLETE_POSITIVE`.
- Phase 5I post-rename probe emits `COMPLETE_POSITIVE`.
- Phase 5K-B code is deployed.

## Steps

1. Run:

   ```powershell
   python scripts\d-phase5k-live-path-parity-probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   ```

2. If both are green, set in `.env`:

   ```text
   CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=true
   ```

3. Restart only the autotrader worker.

4. Watch logs for:

   - no `per_venue_cap_check query failed`
   - no Phase 5K parity mismatches
   - normal Coinbase cap reasons

5. Re-run:

   ```powershell
   python scripts\d-phase5k-live-path-parity-probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   ```

## Rollback

Set:

```text
CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES=false
```

Then restart only the autotrader worker.

## Guardrails

- Do not touch broker/order/stop/reconcile paths.
- Do not change any other flag.
- Do not run migrations.
- Do not restart services other than the autotrader worker.

## Acceptance

- Flag is live in the autotrader worker.
- No cap query failures in logs.
- Phase 5K-A remains `COMPLETE_POSITIVE`.
- Phase 5I remains `COMPLETE_POSITIVE`.
