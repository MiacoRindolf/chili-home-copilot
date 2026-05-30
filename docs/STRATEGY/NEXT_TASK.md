# NEXT_TASK: f-position-identity-phase-5j-remaining-reference-audit

STATUS: PENDING

## Goal

Audit the remaining `trading_trades` references after Phase 5J slices 1-5 and
decide whether any more conversion is safe. Most remaining references are
expected to be compatibility contracts, tests, historical docs, migrations, or
live writer paths that should be deferred.

Keep the `trading_trades` compatibility view and the Python `Trade` ORM class in
place.

## Current Gate State

- Phase 5I closeout: `COMPLETE_POSITIVE`
- Phase 5J slices 1-5 shipped.
- Pid 537 watcher closed:
  - `VERDICT_STATUS=COMPLETE_POSITIVE`
  - `PID_537_N=17`
  - `PID_537_WR=0.6471`
  - `PID_537_PAYOFF=13.0411`
  - `PID_537_STAGE=promoted`
  - scheduled task `CHILI-pid537-watcher`: disabled

## Tasks

1. Generate a current reference inventory:

   ```powershell
   rg -n "\btrading_trades\b" app scripts tests docs
   ```

2. Classify remaining runtime references:
   - **keep:** ORM class/table name, FK metadata, compatibility-view tests
   - **defer:** live writer/order/broker/stop/reconcile paths
   - **convert:** clean read-only scripts/modules with no unrelated dirty work
3. If a safe conversion remains, ship one small slice and rerun:

   ```powershell
   python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

4. If no safe conversion remains, write a closeout report explaining why the
   compatibility view and ORM name stay.

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- Do not touch live writer/order-placement paths.
- Do not edit files with unrelated dirty work unless the current local diff is
  inspected and deliberately preserved.

## Acceptance

- Remaining references are classified with evidence.
- Phase 5I remains `COMPLETE_POSITIVE`.
- No schema-specific worker errors.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-5.md`
- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-pid537-watcher-closeout.md`
- `docs/RUNBOOKS/WATCHER_pid537.md`
