# f-open-position-after-trading-window-exit-trap

STATUS: QUEUED
PRIORITY: P0 SAFETY / EXECUTION RELIABILITY
PROPOSED: 2026-07-01
REQUESTED_BY: operator
SCOPE: live exit lifecycle, Robinhood queued exits, after-hours/premarket session handling, open-position safety

## TL;DR

Study and fix the class of failures where CHILI gets stuck in an open position after the trading window because an exit order is queued, not executable, and the runner keeps polling/re-canceling/replacing or otherwise fails to reach a safe terminal state.

LGPS session `10125` is the live case:

- State: `live_bailout`.
- Position remained open: `44` shares.
- Exit order: `6a446400-373b-4483-81cd-41a8298b1769`.
- Broker order status: `queued`.
- Market session: `closed`.
- New deployed behavior emits `live_exit_queued_poll_deferred`.
- Deferred until: `2026-07-01T11:00:00Z`.

This patch appears to stop the notification/cancel-replace loop, but the broader failure mode still needs a full design audit and replay/soak validation.

## What To Study

1. Broker session semantics

Map Robinhood order states and execution windows:

```text
queued
unconfirmed
confirmed/open
partially_filled
filled
cancelled
rejected
expired
failed
```

Define which states are actionable now, which must be deferred, and which require cancel/replace.

2. Exit liveness state machine

The live runner must distinguish:

```text
exit_submitted_and_active
exit_queued_until_tradable_window
exit_pending_fill
exit_terminal_no_fill_retry_allowed
exit_terminal_flat
exit_terminal_manual_intervention_needed
```

Do not collapse "queued" into "stale failed exit" or into "active open order." Those are different risk states.

3. Open-position risk after window

If CHILI holds a position after the trading window:

- record explicit `overnight_or_closed_session_open_position` telemetry;
- estimate current mark-to-market risk from last valid quote and structural stop;
- avoid notification/cancel/repeg storms;
- decide whether to hold queued exit, schedule next-session exit, or escalate operator alert;
- never assume flat until broker fill or broker-zero reconcile proves it.

4. Broker-order idempotency

Every exit order lifecycle must be idempotent:

- one tracked exit order per exit intent unless terminal no-fill/rejected;
- no duplicate cancel/replace loop while broker says queued;
- retries must be bounded and state-derived, not timer spam;
- event payload must include order status, market session, tradable-at time, expected quantity, filled quantity, and reason.

## Required Validation

Replay/test scenarios:

```text
queued exit during closed session -> defer, do not cancel/repeg loop
queued exit becomes active in premarket -> poll/adopt/fill or retry safely
queued exit rejected before market -> clear tracked order and resubmit if still holding
partial fill during queued/active transition -> update position, keep exit for remainder
broker says filled but local position stale -> reconcile flat
broker says no open order but local still holding -> broker-zero/orphan path
```

Live soak acceptance:

- No notification storm.
- No repeated cancel/replace while order is queued.
- Position state remains honest: not flat until confirmed.
- At tradable window, CHILI either exits, adopts the active order, or emits a clear escalation event.

## Design Principle

This should not be patched as one LGPS special case. Build the correct exit lifecycle abstraction for broker queued/non-tradable windows and prove it end-to-end.

