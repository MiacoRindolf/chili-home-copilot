# Operator flatten of a live momentum session — THROUGH the FSM

**Rule (2026-09-02):** operators (human or Codex) never sell a session's shares
at the broker. A manual broker sale is invisible to the ledger by construction:
the FSM books an `emergency_exit_unpriced` leg (`fill_price: null`), the
outcome row carries only the cycles the FSM priced, and the loss guard
(`risk_policy.load_current_live_loss_history`, which reads
`momentum_automation_outcomes.broker_*`) undercounts the day. CANF 19471:
broker −254.78 vs ledger −145.93 (−108.85 unbooked). Day P&L = broker equity
delta; the ledger must be made to agree by letting the FSM do the selling.

Tool: `scripts/operator_flatten_session.py` (dry-run by default).

```
conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471            # dry-run
conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471 --execute  # set key, wait, stop
conda run -n chili-env python scripts/operator_flatten_session.py --session-id 19471 --stop-only
```

## The FSM exit-authority path (what `--execute` triggers)

| Step | Where | What happens |
|---|---|---|
| 0 | script | Row-locked (`FOR UPDATE NOWAIT`) re-check, then ONE key: `risk_snapshot_json.momentum_live_execution.operator_flatten_requested_utc = <ISO utc>` + audit event `operator_flatten_requested` (`source_node_id=operator_flatten_session_script`). Pause untouched, `entry_*` keys untouched, no synthetic position. |
| 1 | `live_runner.list_runnable_live_sessions` | `_paused_session_has_exit_authority(row)` is True for a held or `live_pending_entry` row carrying the key → the row joins the PRIORITY lane even while operator-paused (paused rows are otherwise excluded from the batch). |
| 2 | `tick_live_session` preamble (~:28446) | `operator_paused and not (exit_authority or owner_recovery.active)` — the key is exit authority, so the silent paused early-return is skipped. (The #1285 adopt-only branch runs first: a filled-while-paused entry is adopted to `live_entered` and sets the same key itself.) |
| 3 | `_service_quote_independent_emergency_exit` (~:30057) | Runs BEFORE any BBO read (a quote outage cannot veto it). `operator_flatten_requested_utc` → `_handle_kill_switch_mid_run("operator_flatten")`. |
| 4 | `_handle_kill_switch_mid_run` (~:28603) | (a) ack-loss recovery of the entry by CID; (b) exact cancel of a still-open entry → `live_order_cancelled` (a 422 on an already-filled order is a no-op); (c) adopt any fill of that entry → `position`, `live_pending_entry → live_entered`; (d) signed broker quantity read (`_broker_position_basis_for_emergency`); (e) broker > 0 → the broker exit chokepoint: `live_exit_submitted` → `live_exit_filled` (PRICED, `emergency_exit_authority.client_order_id = chili_ml_x_<sid>_<digest>`); broker == 0 with no in-flight authority → `_record_emergency_unpriced_fill` → `live_emergency_exit_unpriced` (`note: broker_zero_without_exact_exit_fill`, the shape a manual sale leaves behind). Returns True only on a completed proof. |
| 5 | ~:30078 | `operator_flatten_executed` (done) or `operator_flatten_pending` (cancel pending / ack-loss / identity mismatch / broker read unknown — the persisted `emergency_exit_authority` makes the NEXT tick continue from where it stopped). On done: key popped, `_transition_completed_emergency`: held → `live_exited`; pending with no quantity → `watching_live` → `live_cancelled`. |
| 6 | script | Polls `trading_automation_events` for `operator_flatten_executed` (prints every `live_order_cancelled` / `live_exit_*` / `live_emergency_exit_*` / `operator_flatten_*` / `live_tick_operator_paused_block` it sees), then `automation_query.stop_automation_session(user_id, session_id)`. |
| 7 | `stop_automation_session` | For a `live_exited` row with no position and a resolved claim: `_flatten_live_session_for_stop` → `_reaper_broker_position_truth` (Alpaca `get_position_quantity`, served by the venue factory since #1286) → `broker_flat_confirmed` → `live_cancelled`, `ended_at`, pause cleared, `session_stopped`, outcome row via the terminal transition. `pending: broker_flat_confirmation` / `durable_alpaca_entry_claim_reconcile` ⇒ exit code 2, re-run `--stop-only` after the next tick. |

Related but NOT this path: `request_flatten_session` (the FLATTEN button) sets
the same key but refuses `live_pending_entry` (`not_flattenable`) — the 19471
shape. `stop_automation_session` on a pending row with a CID-bearing durable
claim only pauses (`pending: durable_alpaca_entry_claim_reconcile`) and does
not tick — the B3 wedge. The script covers both gaps.

## The lane MUST be running

The tick is the only executor. The script reads the durable live-loop
heartbeat (`lane_health.live_runner_loop_control_health`, the same read
auto-arm admission uses):

* heartbeat fresh → `--execute` sets the key and waits (`--wait-seconds`, default 90).
* heartbeat stale/missing → `--execute` is REFUSED unless:
  * `--allow-lane-down`: the key is parked (exit code 3). The first tick after
    the lane relaunches flattens through the FSM. Correct when the price is not
    moving against you.
  * `--inline-tick`: after setting the key the script calls
    `live_runner.tick_live_session(db, sid)` in ITS OWN process (up to
    `--inline-ticks`, default 6, 1 s apart). This is the same in-process tick
    `stop_automation_session` performs (automation_query ~:4234) — still the
    FSM chokepoint, still the FSM's own client_order_id, still booked as a
    priced leg. REFUSED while a lane heartbeat is fresh (two tickers on one row
    is the reaper-race shape). Needs the paper credentials in the process env
    (`.env`), the Alpaca paper posture, and cwd = repo root. This is the tool
    for "the FSM is wedged, the lane is down, and the price is collapsing" —
    the 11:34Z CANF situation.
* driver is the batch scheduler (not the event loop) → the heartbeat cannot
  prove anything; `--execute` proceeds, `--inline-tick` is refused.

## Never

* Sell/cancel at the broker (app, MCP, curl). If it already happened: the
  outcome row will lack that cycle; the broker equity delta is the day's truth
  and the loss guard is short by that cycle until a broker-truth attribution
  lands (open gap, see the 09-02 findings).
* Clear the operator pause first (phantom adoption + deadman POST + recycle ⇒
  possible NEW entry).
* Pop `entry_*` keys or write a synthetic `position`.
* Run two flatten drivers at once (script inline tick + live lane).

## Exit codes

`0` done or dry-run · `1` refused / error · `2` flattened, stop deferred
(`--stop-only` later) · `3` request parked (lane down) · `4` wait timed out
(key stays set; the next tick continues from the persisted authority — read the
`operator_flatten_pending` payload and the `emergency_*` markers first).
