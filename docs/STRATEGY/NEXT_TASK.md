# NEXT_TASK: f-position-identity-phase-5k-a-live-path-parity-probe

STATUS: PENDING

## Goal

Add a read-only Phase 5K-A probe that compares the remaining live decision
surfaces through both names:

- old compatibility view: `trading_trades`
- semantic physical base table: `trading_management_envelopes`

The probe should prove that live capital/promotion/risk readers would receive
identical inputs before any live path is cut over.

## Current Gate State

- Phase 5I post-rename soak: `COMPLETE_POSITIVE`
- Phase 5J reader cleanup: closed
- Phase 5K live-path brief: shipped
- Recommendation: evidence-first parity probe before any live behavior change

## Scope

1. Create a read-only probe script, for example:

   ```text
   scripts/d-phase5k-live-path-parity-probe.py
   ```

2. Compare old-vs-new aggregates for:

   - Coinbase venue cap: open auto-trader Coinbase count and notional
   - PDT guard: true 5-business-day equity day-trade count
   - promotion/cohort realized aggregates by `scan_pattern_id`
   - pattern-quality realized aggregates by `scan_pattern_id`
   - portfolio-risk open exposure by broker/asset kind
   - position-integrity linkage counts for open envelopes

3. Emit a verdict:

   - `COMPLETE_POSITIVE` when all aggregates match
   - `REGRESSION_PARITY` when any mismatch appears
   - `ALERT` when the probe cannot run

4. Add a focused test that pins:

   - the script is read-only
   - both relation names are intentionally present
   - no DDL/DML keywords are present outside comments/strings needed for select

5. Run:

   ```powershell
   python -m pytest tests\test_phase5j_reader_cleanup.py tests\test_phase5i_post_rename_probe.py
   python scripts\d-phase5i-post-rename-soak-probe.py
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dispatch-phase5i-post-rename-soak-probe.ps1
   python scripts\d-phase5k-live-path-parity-probe.py
   ```

## Guardrails

- Do not edit live broker/order/stop/reconcile paths.
- Do not edit dirty local files.
- Do not drop or replace the `trading_trades` compatibility view.
- Do not rename the Python `Trade` ORM class.
- No database writes, service restarts, migrations, or broker/API calls.

## Acceptance

- Probe emits `COMPLETE_POSITIVE` or a precise mismatch report.
- Phase 5I remains `COMPLETE_POSITIVE`.
- No live trading behavior changes.
