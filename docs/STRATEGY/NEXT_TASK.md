# NEXT_TASK: f-pid537-watcher-closeout-and-phase5j-audit

STATUS: PENDING

## Goal

Close the now-satisfied pid 537 watcher, then audit remaining Phase 5J
`trading_trades` references before deciding whether another reader-cleanup slice
is worth shipping.

Keep the `trading_trades` compatibility view and the Python `Trade` ORM class in
place.

## Current Gate State

- Phase 5I closeout: `COMPLETE_POSITIVE`
- Phase 5J slices shipped:
  - slice 1: brain KPI, management-envelope helper, Coinbase/maker/imminent probes
  - slice 2: decision-packet coverage and divergence analytics
  - slice 3: dynamic priors, ticker-scope autotune, pattern-stats recompute
  - slice 4: realized stats sync and HRP active-position reader
  - slice 5: admin/trading AI health readers, quality-score aggregate, pid537
    watcher, monthly-DD walk-forward script
- Latest pid 537 watcher smoke:
  - `VERDICT_STATUS=COMPLETE_POSITIVE`
  - `PID_537_N=17`
  - `PID_537_WR=0.6471`
  - `PID_537_PAYOFF=13.0411`
  - `PID_537_STAGE=promoted`
- Latest Phase 5I wrapper:
  - `VERDICT_STATUS=COMPLETE_POSITIVE`
  - `LOG_SCHEMA_ERRORS=0`

## Tasks

1. Disable or retarget the `CHILI-pid537-watcher` scheduled task now that the
   positive evidence gate is satisfied and pid 537 is already promoted.
2. Record pid 537 closeout in strategy docs.
3. Audit remaining runtime `trading_trades` references and classify:
   - keep compatibility/ORM/test/migration contracts
   - defer live writer/order/broker/stop paths
   - convert any remaining clean read-only scripts
4. If a clean read-only slice remains, convert it and rerun:

   ```powershell
   python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   ```

## Guardrails

- Do not drop the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- Do not touch live writer/order-placement paths.
- Do not edit files with unrelated dirty work unless the current local diff is
  inspected and deliberately preserved.

## Acceptance

- pid 537 watcher no longer generates redundant action prompts.
- Remaining Phase 5J references are classified.
- Phase 5I remains `COMPLETE_POSITIVE`.
- No schema-specific worker errors.

## References

- `docs/STRATEGY/CC_REPORTS/2026-05-30_f-position-identity-phase-5j-selective-reader-cleanup-slice-5.md`
- `scripts/d-pid537-watcher.py`
- `docs/RUNBOOKS/WATCHER_pid537.md`
