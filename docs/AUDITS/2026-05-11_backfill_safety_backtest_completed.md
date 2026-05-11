# Backfill safety: `backtest_completed` (cpcv_gate handler)

> Phase 1c (`f-brain-event-kind-backfill`) pre-flight memo.
> Author: 2026-05-11.
> Companion: `docs/AUDITS/2026-05-11_backfill_safety_breakout_alert_resolved.md`.
> Runbook: `docs/runbooks/BRAIN_EVENT_BACKFILL.md`.
> Backfill script: `scripts/brain-event-backfill.ps1`.
> Backfill marker: `payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'`.

## Scope

1,055 historical `brain_work_events` rows with
`event_kind='outcome'`, `event_type='backtest_completed'`, `status='done'`.
These were enqueued by the legacy producer path that wrote them
born-terminal. Flipping them back to `status='pending'` exposes them
to the `cpcv_gate` handler at
`app/services/trading/brain_work/handlers/cpcv_gate.py`
(`handle_backtest_completed`).

This is the **drought-relief payload** of Phase 1c: the
`cpcv_n_paths` drought the parent initiative was created to fix is
fed by cpcv_gate verdicts, which only run when these events claim.

## Idempotency

`cpcv_gate` has two layers of replay safety:

1. **Lifecycle short-circuit.** Lines 76–81 of `cpcv_gate.py`:

   ```python
   if old_lc in ("promoted", "retired"):
       logger.info(... "skip (terminal)" ...)
       return
   ```

   Any backfilled row whose pattern has already advanced to
   `promoted` or `retired` (whether via the original event or any
   later organic event) is a no-op. The handler exits cleanly without
   mutating state.

2. **Deterministic recomputation for candidate patterns.** When the
   lifecycle is still pre-terminal (`candidate`, `backtested`,
   `eligible_promotion`), the handler recomputes the CPCV eval from
   the current `prediction_trade_rows` (PTR) table. Output is a
   function of inputs (the PTR rows) — re-running with the same PTR
   state produces the same `cpcv_*` field values. Re-writing
   identical numbers is a no-op at the row level.

3. **Downstream dedupe by source event id.** When the gate passes,
   the handler enqueues `pattern_eligible_promotion` with

   ```python
   dedupe_key=f"eligible:cpcv:{pid}:{ev.id}"  # cpcv_gate.py:152
   ```

   Because `ev.id` is the source row's id (stable across replays),
   the same backfilled row produces the same dedupe key — and the
   open-key unique index (`uq_brain_work_events_open_dedupe`)
   prevents a duplicate live promotion event from being created if a
   competing one is already in `pending/processing/retry_wait`.

   The backfill script's candidate query also includes a NOT EXISTS
   guard against any row sharing the same `dedupe_key` already in a
   non-terminal state, so we won't even attempt to flip a row that
   would collide on insert-side dedupe.

Net: replaying these rows is safe. Already-graduated patterns
no-op; candidate patterns get recomputed deterministically; the
downstream promote path is event-id-keyed.

## Side effects

When the lifecycle is candidate / backtested:

- `scan_patterns.cpcv_*` numeric columns (median sharpe, deflated
  sharpe, paths, etc.) via `cpcv_eval_to_scan_pattern_fields` (line
  132).
- `scan_patterns.lifecycle_stage` may transition `candidate ->
  backtested` (line 140), `lifecycle_changed_at` set.
- `scan_patterns.promotion_status` may transition to
  `eligible_promotion` (line 143) or to a gate-failure status (lines
  168+).
- `cpcv_shadow_eval` audit row appended via `persist_cpcv_shadow_eval`
  (line 127).
- One new `brain_work_events` row of type
  `pattern_eligible_promotion` per pattern that passes the gate
  (line 149).

## Throughput estimate

The dispatcher claims up to `brain_work_cpcv_gate_batch_size` rows
per round (default 8). Cycle cadence in prod has been 25–90 min.

At a steady 30-min cadence:
- 1055 rows / 8 per round = ~132 rounds.
- 132 rounds × 30 min ≈ 66 hours of dispatcher time.

The operator can compress this by:
1. Raising `brain_work_cpcv_gate_batch_size` temporarily.
2. Running the backfill script in larger waves (`-MaxRows 200`)
   while keeping `BatchSize=8` so the dispatcher still drains
   between waves.

The 30 s inter-batch sleep in the backfill script is independent of
the dispatcher cadence — it just paces the `status='done' -> 'pending'`
flips so we don't dump 1055 pending rows at the queue in a single
second.

## Rollback

If the backfill produces unexpected state, undo all
`backtest_completed` flips with:

```sql
UPDATE brain_work_events
SET status = 'done',
    processed_at = CURRENT_TIMESTAMP,
    attempts = 0,
    lease_holder = NULL,
    lease_expires_at = NULL
WHERE domain = 'trading'
  AND event_kind = 'outcome'
  AND event_type = 'backtest_completed'
  AND payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'
  AND status IN ('pending', 'retry_wait', 'processing');
```

Side effects from already-fired handlers are not reversed by this
rollback (the cpcv_* fields, lifecycle transitions, and any
`pattern_eligible_promotion` rows that were created remain). To
unwind those, follow the rollback section of
`docs/runbooks/BRAIN_EVENT_BACKFILL.md`.

## Gated event types (DO NOT confuse with this memo)

This memo authorizes replay of `backtest_completed` only.

**`market_snapshots_batch` is GATED.** Its target handler
(`mine_patterns` via `regime_ledger`) has no event-level dedupe —
the Phase 1b runbook (`docs/runbooks/BRAIN_WORK_EVENT_KIND.md`)
flagged this and Phase 1c (this file) reaffirms it. Do not run the
backfill script against `-EventType market_snapshots_batch` until
the `mine_patterns` inner contract is verified. The script will warn
and pause 5 s before any such run; the runbook section "GATED event
types" describes the verification gate.
