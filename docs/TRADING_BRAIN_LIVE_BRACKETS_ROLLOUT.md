# Trading Brain - Live brackets + stop reconciliation rollout

Phase G ships the substrate and observability layer that moves stop /
target enforcement from purely client-side polling
(`stop_engine.evaluate_all`) toward server-side bracket orders at the
broker, and adds a continuous reconciliation job that detects drift
between local trade state and the broker's view (orphaned stops,
missed fills, cancelled children, quantity mismatches).

Phase G is **strictly shadow-only**. The authoritative cutover (Phase
G.2) is deferred; its own plan must explicitly freeze the venue
adapter protocol extension and TCA reconciliation against bracket
children before any live broker writes are enabled.

## Shipped in Phase G

* Migration `133_live_brackets_reconciliation`:
  * `trading_bracket_intents` (one row per live trade bracket intent)
  * `trading_bracket_reconciliation_log` (append-only sweep discrepancies)
* ORM models `BracketIntent`, `BracketReconciliationLog`
  (`app/models/trading.py`).
* Pure modules:
  * `app/services/trading/bracket_intent.py::compute_bracket_intent`
    reuses `stop_engine._compute_initial_stop` so bracket intents are
    mathematically consistent with the live stop engine.
  * `app/services/trading/bracket_reconciler.py::classify_discrepancy`
    exhaustive match over `agree | orphan_stop | missing_stop |
    qty_drift | state_drift | price_drift | broker_down | unreconciled`.
* DB writer / service:
  * `bracket_intent_writer.upsert_bracket_intent` - idempotent,
    refuses to overwrite `authoritative_submitted` state in shadow.
  * `bracket_intent_writer.mark_reconciled` / `bracket_intent_summary`.
  * `bracket_reconciliation_service.run_reconciliation_sweep` - **read-
    only** against the broker, raises if invoked in `authoritative`
    mode in Phase G.
  * `bracket_reconciliation_service.bracket_reconciliation_summary` for
    the diagnostics endpoint.
* Ops logs:
  * `[bracket_intent_ops]` with events `intent_write`,
    `intent_write_skipped`, `mark_reconciled`.
  * `[bracket_reconciliation_ops]` with events `discrepancy`,
    `sweep_summary`.
* Emitter call-site: `stop_engine._maybe_emit_bracket_intent`, called
  exactly once per evaluation inside `evaluate_all` after
  `_apply_stop_to_trade`. Paper trades are skipped via the
  `broker_source` guard.
* APScheduler job `bracket_reconciliation` registered when
  `brain_live_brackets_mode != off`; interval defaults to 60 seconds,
  gate enforces that the sweep refuses to run in `authoritative` mode.
* Diagnostics endpoints:
  * `GET /api/trading/brain/bracket-intent/diagnostics` - frozen shape
    `{mode, lookback_hours, intents_total, by_state, by_broker_source,
    latest_intent}`.
  * `GET /api/trading/brain/bracket-reconciliation/diagnostics` -
    frozen shape `{mode, lookback_hours, recent_sweeps_requested,
    rows_total, by_kind, by_severity, last_sweep_id,
    last_observed_at, sweeps_recent}`.
* Config flags (`app/config.py` + `.env`):
  * `BRAIN_LIVE_BRACKETS_MODE=shadow`
  * `BRAIN_LIVE_BRACKETS_OPS_LOG_ENABLED=true`
  * `BRAIN_LIVE_BRACKETS_RECONCILIATION_INTERVAL_S=60`
  * `BRAIN_LIVE_BRACKETS_PRICE_DRIFT_BPS=25.0`
  * `BRAIN_LIVE_BRACKETS_QTY_DRIFT_ABS=0.000001`
* Tests: `tests/test_bracket_intent_compute.py` (15),
  `tests/test_bracket_reconciler_classify.py` (16),
  `tests/test_bracket_intent_writer.py` (8),
  `tests/test_bracket_reconciliation_service.py` (9) -
  **48 Phase-G-specific tests pass**.
* Docker soak: `scripts/phase_g_soak.py` verifies migration,
  shadow-mode settings, idempotent emitter, reconciliation sweep with
  agree + idempotency, diagnostics shape, and
  authoritative-mode refusal. Run inside the `chili` container.
* Release blocker scripts:
  * `scripts/check_live_brackets_release_blocker.ps1` - fails on any
    `[bracket_intent_ops] event=intent_write mode=authoritative` log
    line and supports an optional diagnostics-JSON gate.
  * `scripts/check_bracket_reconciliation_release_blocker.ps1` - fails
    on any `[bracket_reconciliation_ops]` line with `event=submit /
    cancel / modify` or `mode=authoritative`, and supports broker-down
    fraction threshold plus a minimum-rows gate.

## Rollout ladder

Phase G stays on `shadow` across all environments. Phase G.2 will
extend this ladder; do **not** skip steps.

| Step | `BRAIN_LIVE_BRACKETS_MODE` | Reads broker? | Writes broker? | Writes `trading_bracket_intents`? | Writes `trading_bracket_reconciliation_log`? |
|------|---------------------------|---------------|----------------|-----------------------------------|-----------------------------------------------|
| Off  | `off`                      | No            | No             | No                                | No                                            |
| Shadow | `shadow`                 | Yes           | **No**         | Yes (`shadow_logged`)             | Yes                                           |
| Compare | `compare` *(reserved)*  | Yes           | **No**         | Yes (`shadow_logged`)             | Yes (`mode=compare`)                          |
| Authoritative | `authoritative`  | Yes           | Yes *(Phase G.2)* | Yes (`authoritative_submitted`) | Yes (`mode=authoritative`)                    |

Phase G ships `off` → `shadow`. Do not flip to `authoritative` in
Phase G; the reconciliation service refuses (`RuntimeError`) and the
release-blocker scripts will exit non-zero.

## Rollback

Flip `BRAIN_LIVE_BRACKETS_MODE=off` and
`docker compose up -d --force-recreate chili brain-worker`. No code
rollback required: shadow never writes to the broker. Existing
`trading_bracket_intents` rows are data-only and can stay.

## Mandatory release blockers

1. `.\scripts\check_live_brackets_release_blocker.ps1` on the last 30
   minutes of `chili` logs must exit 0.
2. `.\scripts\check_bracket_reconciliation_release_blocker.ps1` on the
   last 30 minutes of `chili` logs must exit 0.
3. `scripts/phase_g_soak.py` must pass inside the `chili` container.
4. `tests/test_scan_status_brain_runtime.py` must stay green - the
   frozen `scan_status.brain_runtime` contract is not extended in
   Phase G.
5. `BRAIN_LIVE_BRACKETS_MODE` visible as `shadow` inside the running
   `chili` container (`docker compose exec chili env | Select-String
   BRAIN_LIVE_BRACKETS_MODE`).

## Monitoring

* Look for `[bracket_intent_ops] event=intent_write mode=shadow` on
  every live trade that enters the stop engine's initial-stop branch.
* Look for `[bracket_reconciliation_ops] event=sweep_summary
  mode=shadow` roughly once per
  `BRAIN_LIVE_BRACKETS_RECONCILIATION_INTERVAL_S` seconds.
* The diagnostics endpoints are safe for read-only observation
  dashboards; they do not trigger new sweeps.

## Non-goals (for Phase G)

* Placing server-side stop / stop-limit / OCO / bracket orders at any
  broker. That is **Phase G.2**.
* Changing `stop_engine`'s state machine, cooldowns, or auto-execute
  logic. Untouched.
* Moving `paper_trading` to bracket intents. Untouched.
* Extending the venue adapter protocol. That is **Phase G.2**.

## Known gaps to cover in Phase G.2

* Venue adapter protocol extension for stop / stop-limit / OCO /
  bracket primitives (Robinhood + Coinbase + Kraken).
* Authoritative writer that places / cancels / modifies bracket
  children, guarded by governance kill-switch and risk dial (Phase I).
* TCA reconciliation against bracket children (fills, partial fills,
  replacements) and its feedback into Phase F venue-truth telemetry.
* Multi-leg OCO semantics (cancel-on-fill of sibling legs).
* Per-broker bracket policy (Robinhood lacks native OCO; Coinbase
  supports stop orders but not brackets).

Phase G is observability + the data substrate for the future
authoritative cutover, and nothing else.
