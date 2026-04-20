# Reconciliation drift runbook

What to do when the bracket reconciliation sweep flags a non-agree
classification. Owned by whoever is oncall for the trading brain; this
document is the first stop before touching live orders.

## 1. Where signals come from

There are three watchdogs on the reconciliation path, each covering a
different failure mode. Know which one fired before you react.

| Watchdog | Source log prefix | Fires on | Default |
|---|---|---|---|
| Missing-stop (age-based) | `[bracket_watchdog]` | `missing_stop` / `orphan_stop` older than `chili_bracket_watchdog_stale_after_sec` (default 300s) | Off (feature flag) |
| Drift escalation (count-based) | `[drift_escalation]` | Same non-agree kind for N consecutive sweeps (default 5) in last 60 min | Off (feature flag) |
| Execution-event lag | `[execution_event_lag]` | P95 of `recorded_at - event_at` on `trading_execution_events` crosses `warn` (15s) / `error` (60s) | On |

The sweep itself logs to `[bracket_reconciliation]`; individual
classifications of non-agree rows land in
`trading_bracket_reconciliation_log` and can be queried directly.

## 2. Classification decision tree

Use this in order. Don't skip — the questions below the ones that fail
assume the one above passed.

### a) Is the broker reachable?

```
kind = broker_down
```

Check [broker_service.is_connected()](../app/services/broker_service.py) for
Robinhood and coinbase_service for Coinbase. If the adapter can't talk
to the venue, every reconciliation result in that sweep is
`broker_down` — this is not a drift bug, it's a connectivity bug.
Action: fix auth / network first, ignore the downstream escalations
they caused.

### b) Is there a bracket intent for the trade?

```
trade open + no intent           → agree path (no-op; sweep skips)
trade open + intent + no broker stop → missing_stop
trade not open + broker stop     → orphan_stop (serious — we don't own the position anymore)
```

**`missing_stop` is expected during Phase G** — the sweep runs but no
server-side stop exists. Phase G.2 writer (now default ON) should
auto-place a stop for these; if `[bracket_writer_g2]` is logging
`place_missing_stop` actions, the system is self-healing. If drift
persists for 5+ sweeps, the placement is failing — see step 4.

**`orphan_stop` is always an incident.** A position we no longer own
has a live stop that could fire against future re-entry. Action:

1. Cancel the broker-side stop manually via the Robinhood dashboard or
   `rh.orders.cancel_stock_order(order_id)`.
2. Flip the intent's `intent_state` to `authoritative_closed` so the
   sweep stops re-flagging.

### c) Does the quantity match?

```
kind = qty_drift
```

Check `delta_payload.drift_kind`:

- **`partial_fill`**: broker has fewer shares than we intended. Phase
  G.2 writer (`resize_stop_for_partial_fill`) should auto-resize the
  stop. Monitor for `[bracket_writer_g2] resize_stop` log lines. If
  they're failing, go to step 4.
- **`over_fill`**: broker has MORE shares than we intended. This should
  never happen — escalate immediately. Likely a stuck fill or a
  duplicate order (which the idempotency store should have prevented).
- **`broker_flat`**: we think we have a position, broker shows zero.
  Could be: (1) the fill was rejected after we recorded it locally, or
  (2) a manual close outside the app. Check `trading_execution_events`
  for the most recent `fill` / `cancel` row on this `broker_order_id`.

### d) Does the stop/target price match?

```
kind = price_drift
```

The local intent says stop at X, broker has stop at Y. Tolerance is
`chili_bracket_price_drift_bps` (default 25 bps). Usually this means
the Phase G.2 writer placed a stop then we updated the local intent
without re-placing; next sweep should trigger a resize. If drift
persists, disable `chili_bracket_writer_g2_partial_fill_resize` and
investigate before re-enabling.

### e) Does the intent state match what's live?

```
kind = state_drift
```

Local says `authoritative_submitted`, broker says the order is
`cancelled`/`rejected`/`expired`. Rare; indicates a cancel happened
outside the writer's control (operator action, broker-side kill, TIF
expiry). Action: flip the intent to `authoritative_closed` and
investigate the venue-side cancel.

## 3. Kill switches

Every writer has an env override that takes effect on the next
scheduler tick without redeployment:

| Action | Env var | Effect |
|---|---|---|
| Entire G.2 writer | `CHILI_BRACKET_WRITER_G2_ENABLED=0` | Reconciler goes back to read-only |
| Just partial-fill resize | `CHILI_BRACKET_WRITER_G2_PARTIAL_FILL_RESIZE=0` | No more cancel+replace on qty_drift |
| Just missing-stop placement | `CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=0` | Reconciler flags missing_stop but won't auto-place |
| The reconciliation sweep itself | `BRAIN_LIVE_BRACKETS_MODE=off` | Sweep returns empty summary |
| All trading | `KILL_SWITCH` via `/trading/kill-switch` endpoint | Blocks all automated trades |

## 4. When the Phase G.2 writer is failing

The escalation watchdog fires when the same kind persists for 5+
sweeps — that's the signal the writer is failing to repair, not that
drift is transient.

```
tail -f logs | grep "bracket_writer_g2"
```

Expected happy-path log lines:
- `resize_stop intent=X ticker=Y ...` (resize succeeded)
- `place_missing_stop intent=X ticker=Y ...` (placement succeeded)

Failure log lines to watch for:
- `PRIOR STOP CANCELLED BUT REPLACEMENT FAILED` — **critical.** The
  position is unprotected for at least one sweep interval. Either
  manually place a stop at the broker or close the position.
- `cancel_order raised` / `cancel failed` — broker rejected the cancel
  (usually because the order is already filled / already cancelled).
  Usually resolves on next sweep.
- `place_limit_order_gtc raised` — broker rejected the new stop.
  Market closed, insufficient buying power, or adapter bug.

## 5. Quick queries

### Most recent non-agree classifications

```sql
SELECT observed_at, trade_id, ticker, broker_source, kind, severity,
       delta_payload->>'drift_kind' AS drift_kind
FROM trading_bracket_reconciliation_log
WHERE kind <> 'agree'
  AND observed_at >= NOW() - INTERVAL '1 hour'
ORDER BY observed_at DESC
LIMIT 50;
```

### Intents in persistent drift

```sql
SELECT bi.id, bi.ticker, bi.intent_state, bi.last_diff_reason,
       AGE(NOW(), bi.last_observed_at) AS observed_ago
FROM trading_bracket_intents bi
WHERE bi.intent_state NOT IN ('reconciled', 'authoritative_closed')
ORDER BY bi.last_observed_at ASC NULLS FIRST
LIMIT 20;
```

### Execution-event lag right now

```sql
SELECT venue,
       percentile_cont(0.95) WITHIN GROUP (
         ORDER BY EXTRACT(EPOCH FROM (recorded_at - event_at)) * 1000
       ) AS p95_ms,
       COUNT(*) AS samples
FROM trading_execution_events
WHERE event_at IS NOT NULL
  AND recorded_at > event_at
  AND recorded_at >= NOW() - INTERVAL '5 minutes'
GROUP BY venue;
```

## 6. Escalation contacts

- **Self-trading (single-tenant deployment):** the operator is also
  the user. Decide whether to pause the reconciler, disable a specific
  writer action, or close positions manually.
- **When in doubt:** flip `BRAIN_LIVE_BRACKETS_MODE=off` first. The
  sweep stops, no writer actions fire, the position sits as it was.
  Debug with that safety on, not off.
