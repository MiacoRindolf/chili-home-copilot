# 2026-07-01 TC/LHAI Live Loss Audit

Scope: live Ross/equity momentum sessions that fired after the Ross hard-gate recovery. This is not a broad-universe leak audit; TC and LHAI were small-cap/Ross-score candidates. The failure class is execution/lifecycle quality.

## Session 10343: TC

- Symbol/session: `TC`, `trading_automation_sessions.id=10343`
- State: `live_finished`
- Candidate source/shape:
  - First cycle trigger: `ross_breakout_starter_tick`
  - Later cycle trigger: `abcd_break_tick_ok`
  - Micro frame recorded: `5m`, not a clean tick/sub-second Ross entry loop
- First entry cycle:
  - Candidate: `2026-07-01T17:31:37.514127Z`
  - Pending place: `2026-07-01T17:31:39.149496Z`
  - Historical stale clock refresh event: `2026-07-01T17:31:49.262248Z`
  - Recorded pending age: `10.113s`, max allowed: `6.0s`
  - Submitted: `2026-07-01T17:31:50.629692Z`
  - Filled: `2026-07-01T17:31:54.513450Z`, qty `123`, approx entry `4.9999`
  - Exit: `2026-07-01T17:32:02.545593Z`, approx exit `5.00`, realized `+$0.0123`
- Reprocessed/later entry cycle:
  - Candidate: `2026-07-01T17:35:16.022228Z`
  - Pending place: `2026-07-01T17:35:17.398697Z`
  - Historical stale clock refresh event: `2026-07-01T17:35:28.292113Z`
  - Recorded pending age: `10.893s`, max allowed: `6.0s`
  - Submitted: `2026-07-01T17:35:29.818076Z`
  - Filled: `2026-07-01T17:35:34.879287Z`
  - Exit: `2026-07-01T17:35:41.260224Z`, approx exit `4.97`, realized about `-$1.2768`
- Classification:
  - Primary issue: stale entry latency was incorrectly refreshed instead of blocked in historical runtime.
  - Secondary issue: same-session/restart lifecycle allowed a second processed entry path after the initial completed cycle.
- Required code gate:
  - Pre-submit stale path must rewatch/block, never refresh `entry_pending_place_utc`.
  - Restart/idempotency should not reprocess an already completed/terminal entry cycle.

## Session 10344: LHAI

- Symbol/session: `LHAI`, `trading_automation_sessions.id=10344`
- State: `live_finished`
- Candidate source/shape:
  - Admission: `2026-07-01T17:31:40Z`
  - Repeated wait reason: `volume_below_1p5x_avg`
  - Candidate trigger: `abcd_break_tick_ok`
  - Micro frame recorded: `5m`
- Timeline:
  - Candidate: `2026-07-01T17:32:26.330451Z`
  - Pending place: `2026-07-01T17:32:27.255624Z`
  - Size-floor event: `2026-07-01T17:32:30.445589Z`, reason `hard_blocker`
  - Submitted: `2026-07-01T17:32:32.951565Z`
  - Filled: `2026-07-01T17:32:35.621118Z`, qty `181`, approx entry `1.56`
  - Bailout: `2026-07-01T17:33:25.019364Z`, reason `breakout_failed_fast_bail`
  - Exit fill: `2026-07-01T17:33:33.376591Z`, approx exit `1.5314`, realized `-$5.1766`
- Classification:
  - Primary issue: setup/execution quality, not broad-universe leak. The entry was admitted after repeated volume waits, on a 5m-derived ABCD trigger, then failed.
  - Secondary issue: a `hard_blocker` size-floor event did not clearly prevent later submission.
- Required code gate:
  - Ross lane should prefer tick/tape event revalidation over 5m continuation logic for live entries.
  - Historical `hard_blocker` size-floor wording must not survive as an ambiguous submit blocker.
- Current fix status:
  - 5m live ABCD without tick/tape revalidation is blocked by the current live-shape guard.
  - The size-floor skip reason is now `hard_reducer_respected`, not `hard_blocker`, and tests assert it is not labeled as an order blocker.

## Session 10183: LHAI Earlier Loss

- Symbol/session: `LHAI`, `trading_automation_sessions.id=10183`
- State: `live_cancelled`
- Candidate source/shape:
  - Setup: `tape_confirmed_hold`
  - Micro frame recorded: `15s`
  - IQFeed micro present: `micro_has_iqfeed=True`
- Timeline:
  - Tape hold fired: `2026-07-01T13:26:16.374319Z`
  - Pending place: `2026-07-01T13:26:20.703784Z`
  - Submitted: `2026-07-01T13:27:13.794867Z`
  - Filled: `2026-07-01T13:27:20.123111Z`
  - Bailout: `2026-07-01T13:27:24.167483Z`, reason `instant_bid_below_fill_cut`
  - Exit fill: `2026-07-01T13:27:34.222206Z`, approx exit `1.76`, realized `-$3.95`
- Post-exit counterfactual:
  - `counterfactual_target_hit=True`
  - `outcome_class=shakeout`
  - `stop_too_tight=True`
  - `post_exit_mfe_pct=14.2045`
- Classification:
  - Primary issue: exit/smart-hold/bailout was too tight after fill.
  - Secondary issue: submit latency was too slow for Ross-style scalp timing.
- Required code gate:
  - Fast bailout should be adaptive to tape/structure and not cut a still-valid frontside move immediately because the bid briefly slips below fill.

## Regression Coverage Now Required

- `test_pre_submit_stale_path_blocks_internal_latency_without_refreshing_clock` proves stale pre-submit blocks and preserves the old pending timestamp.
- Ross universe final-boundary tests prove broad equities cannot enter live Ross lane from generic `live_eligible` rows.
- IQFeed/event admission tests prove Ross candidates can arm and tick without the scheduler batch path.
- Runtime verifier tests prove a disabled placeholder cannot silently occupy the canonical momentum worker name.
- Ordered lifecycle audit tests prove add/remainder/exit stages are evaluated in sequence, so future TC/LHAI-style traces can distinguish stale entry, missing trailing arm, unresolved add submit, partial exit, and final exit instead of collapsing them into flat counts.

## 2026-07-01 Follow-Up Verification

Read-only Replay v3/export audit scoped to `--session-ids 10343,10344,10183` returned `ok=true`.

- Inputs: 3 session rows, 151 setup-trace/audit events, 8 broker/outcome rows, execution family `robinhood_agentic_mcp`.
- Lifecycle summary: 3 sessions with entry fill, 3 sessions with exit fill, 0 sessions with trailing armed, 0 add fills.
- Scheduler replay result: no selectable sessions, all 3 terminalized as historical pre-entry/terminal rows. This is correct and prevents overclaiming scheduler/PnL impact from terminal historical rows.
- Setup trace result: 55 traces, 0 setup-trace findings.

Current code/test status:

- Stale pre-submit refresh is now guarded by `test_pre_submit_stale_path_blocks_internal_latency_without_refreshing_clock`.
- 5m-derived live ABCD without tick/tape revalidation is guarded by `test_ross_live_entry_shape_blocks_5m_abcd_without_tick_tape_revalidation`.
- CANF-style tick first-pullback live shape is allowed by `test_ross_live_entry_shape_allows_tick_first_pullback` and live tick-scalp same-runner entry coverage.
- Historical `hard_blocker` size-floor ambiguity is now renamed/covered as `hard_reducer_respected`; `test_a_setup_size_floor_hard_reducer_is_not_labeled_order_blocker` proves the floor skip is not labeled as an order blocker.
- Too-tight instant bid cut now has structural suppression coverage for Ross tick entries via `test_ross_instant_bid_cut_suppresses_first_wick_above_structure` and the structural-stop breach counter-test.
- Latest incident-class validation passed 128 tests across transcript bridge, Ross event admission, tick scalp, runtime guard, feature flags, setup trace, live replay export, and Replay v3 sizing/PnL; the live-runner-targeted incident slice passed 12 tests covering stale pre-submit, same-session re-entry/cooldown, hard reducer, 5m ABCD, instant-bid suppression, and adapter-unavailable batch starvation.

Remaining proof boundary:

- The historical rows cannot prove the new add window because none reached `live_trailing_armed`. Fresh post-deploy telemetry must show `live_trailing_armed` and any `pullback_add` / `micropullback_reentry` / `pyramid` / `flag_breakout_add` events before claiming add-path EV.
