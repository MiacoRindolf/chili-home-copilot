# Brain work event-kind unification (Phase 1b)

Operator runbook for the `chili_brain_outcome_claimable_enabled` flag —
the unified `brain_work_events` queue that lets outcome-kind events
transit the same `pending → processing → done` lifecycle as work-kind
events.

**Author:** 2026-05-11 (Phase 1b of f-adaptive-promotion-architecture).
**Brief:** `docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md`
**Parent memo:** `docs/AUDITS/2026-05-11_dispatcher_silence.md`

## What this changes

`enqueue_outcome_event` (`app/services/trading/brain_work/ledger.py`) used to
write `status='done', processed_at=now()` in one INSERT — the outcome
row was born terminal. The dispatcher's `claim_work_batch` SQL filtered
`event_kind='work'`, so outcomes never claimed and the 9 handlers
(cpcv_gate, mine, promote, demote, regime_ledger, pattern_stats,
breakout_outcomes, live_drift, execution_robustness) never fired against
production traffic.

When `chili_brain_outcome_claimable_enabled=True`:

* New outcome rows are born `status='pending'`, `processed_at=NULL`,
  `max_attempts=brain_work_max_attempts_default`.
* `claim_work_batch` drops the `AND event_kind='work'` filter — outcomes
  and work rows transit the same lifecycle.
* `release_stale_leases` drops the same filter, so an outcome row whose
  lease expires (handler hang, container restart) is recoverable.
* Historical `status='done'` rows stay ineligible by the
  `status IN ('pending', 'retry_wait')` predicate. Phase 1c
  (`f-brain-event-kind-backfill.md`) is the controlled mechanism to
  bring those forward.

Flag default is `False`. Merge produces zero behaviour change.

## How to flip the flag

Pydantic `BaseSettings` does NOT auto-refresh from the database. The flip
sequence is ALWAYS:

1. **UPDATE the setting in Postgres**

   ```sql
   UPDATE trading_settings
   SET chili_brain_outcome_claimable_enabled = true
   WHERE id = 1;
   ```

2. **Restart brain-worker**

   ```bash
   docker compose restart brain-worker
   ```

3. **Verify in logs**

   ```bash
   docker compose logs --tail 200 brain-worker | grep -E \
     "\[brain_work:(cpcv_gate|mine|promote|demote|pattern_stats|regime_ledger|breakout_outcomes|live_drift|execution_robustness)\]"
   ```

   Watch for `ev_id=…` lines from each handler within ~5 minutes (the
   dispatcher runs every 75–90s; cpcv_gate fires on every
   `backtest_completed` event).

DO NOT assume a SQL-only flip propagates — the worker process reads
`settings` once at import.

## Rollout sequence

Mirrors the brief's 4 stages:

1. **Stage 0: ship at flag-OFF (this PR).** Migration 238 lands the
   partial index; no behaviour change. Phase 1b is merged.
2. **Stage 1: enable in dev.** Operator flips the flag on a dev
   brain-worker, watches one full dispatch cycle. Verifies handler log
   prefixes appear and `brain_work_events` shows new pending → done
   transitions.
3. **Stage 2: enable in shadow / staging.** 24-hour soak. Verify no
   handler retries pile up (dead-letter < 1% of throughput). Watch
   `pending_by_type` SQL snapshot every hour.
4. **Stage 3: enable in production.** Operator flip in `chili` DB +
   brain-worker restart. 24-hour observation before Phase 1c starts.

## How to roll back

If a handler misbehaves under flag-on:

1. **Flip the flag off** (UPDATE trading_settings, restart brain-worker).
2. **Drain status='pending' outcome rows** enqueued during flag-on so
   they don't strand. They became unreachable as soon as the flag
   flipped (claim path goes back to `event_kind='work'`).

   ```sql
   UPDATE brain_work_events
   SET status = 'done', processed_at = CURRENT_TIMESTAMP
   WHERE event_kind = 'outcome'
     AND status IN ('pending', 'retry_wait', 'processing')
     AND created_at >= '<flag-on-timestamp>';
   ```

   Replace `<flag-on-timestamp>` with the time the flag was enabled in
   that environment.

3. **Verify no dead leases.** A row in `status='processing'` with
   `lease_expires_at < now()` would not be reclaimed by the flag-off
   `release_stale_leases` (work-only filter). The UPDATE above covers
   the case.

## Observability

### Per-handler log prefixes

Each handler emits at `INFO` on each successful run. Grep brain-worker
logs:

| Handler | Prefix |
|---|---|
| cpcv_gate | `[brain_work:cpcv_gate]` |
| mine | `[brain_work:mine]` |
| promote | `[brain_work:promote]` |
| demote | `[brain_work:demote]` |
| regime_ledger | `[brain_work:regime_ledger]` |
| pattern_stats | `[brain_work:pattern_stats]` |
| breakout_outcomes | `[brain_work:breakout_outcomes]` |
| live_drift | `[brain_work:live_drift]` |
| execution_robustness | `[brain_work:execution_robustness]` |

Dispatcher itself logs at `[brain_work_dispatch]`.

### SQL snapshot — pending by type

```sql
SELECT event_kind, event_type, status, COUNT(*) AS n
FROM brain_work_events
WHERE domain = 'trading'
  AND status IN ('pending', 'processing', 'retry_wait', 'dead')
GROUP BY event_kind, event_type, status
ORDER BY event_kind, event_type, status;
```

Under flag-OFF you should see only `event_kind='work'` rows in these
non-terminal states. Under flag-ON you should see outcome rows
transiting `pending → processing → done` within seconds (capped by
`brain_work_dispatch_batch_size`, default 8 per type per round).

### Note on `get_work_ledger_summary` UI counts

The dashboard's `pending_by_type` summary filters `event_kind='work'`. When
the flag is on, outcome rows in pending state will not appear in that
summary count. UI alignment is Phase 4 of the parent initiative —
acceptable scope for Phase 1b.

## Phase 1c handoff

Phase 1c (`f-brain-event-kind-backfill.md`) controllers the controlled
resurrection of ~4,000 historical `status='done'` outcome rows. **Do not
backfill before Phase 1b has been at flag-ON in production for 24h
without retries piling up.** Phase 1c's pre-flight memos verify the
inner-function idempotency contract of each handler (the
`test_brain_work_handler_idempotency.py` hard gate covers the wrapper
boundary, not the inner functions' own contracts).

The mining handler is the one open gap: `mine_patterns` does NOT have
event-level dedupe. Phase 1c must NOT enable retroactive replay of
historical `market_snapshots_batch` rows until that inner contract is
verified.
