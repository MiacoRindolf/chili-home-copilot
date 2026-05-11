# Backfill safety: `breakout_alert_resolved` (breakout_outcomes handler)

> Phase 1c (`f-brain-event-kind-backfill`) pre-flight memo.
> Author: 2026-05-11.
> Companion: `docs/AUDITS/2026-05-11_backfill_safety_backtest_completed.md`.
> Runbook: `docs/runbooks/BRAIN_EVENT_BACKFILL.md`.
> Backfill script: `scripts/brain-event-backfill.ps1`.
> Backfill marker: `payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'`.

## Scope

2,659 historical `brain_work_events` rows with
`event_kind='outcome'`, `event_type='breakout_alert_resolved'`,
`status='done'`. Largest blast-radius event type in the Phase 1a
orphan set; the runbook puts it last in the recommended order.

Target handler:
`app/services/trading/brain_work/handlers/breakout_outcomes.py`
(`handle_breakout_alert_resolved`), which wraps
`learn_from_breakout_outcomes` in
`app/services/trading/learning.py` (line 4866).

## Idempotency

This handler is **NOT event-level idempotent**. It is
**aggregate-window idempotent**: each event firing re-aggregates the
*entire 180-day window* of resolved BreakoutAlert rows and rewrites
the per-pattern stats. There is no `event_id` or `alert_id` dedupe
inside the handler.

Concretely (`learning.py:4877-4884`):

```python
cutoff = datetime.utcnow() - timedelta(days=180)
alert_q = db.query(BreakoutAlert).filter(
    BreakoutAlert.outcome != "pending",
    BreakoutAlert.outcome_checked_at >= cutoff,
)
# ... order by outcome_checked_at desc, limit 500
```

Implications for backfill:

1. **Each replay is full re-aggregation.** The handler doesn't care
   which event triggered it — every firing rebuilds the same
   buckets over the same 180-day window. Replaying 2659 events
   produces 2659 identical aggregate writes (modulo new resolutions
   creeping in mid-backfill — see "Operational guardrails" below).

2. **ScanPattern aggregates converge immediately.** `win_rate`,
   `avg_return_pct`, and `trade_count` are written by direct
   assignment (no blend) — every run lands at the same value as
   long as the underlying alerts don't change.

3. **TradingInsight.confidence has an EWMA blend** that mathematically
   converges (`learning.py:4998`):

   ```python
   existing.confidence = round(existing.confidence * 0.4 + confidence * 0.6, 3)
   ```

   For a constant aggregate `c`, the recurrence collapses to
   `confidence_k = 0.4^k * confidence_0 + (1 - 0.4^k) * c`. After ~5
   replays the value is within 1% of `c`; after ~10 it's
   indistinguishable. So 2659 replays converge to the same
   fixed point as a single firing, in bounded time.

4. **Source-event-id dedupe is not the safety mechanism here.**
   Unlike `cpcv_gate` (which uses `eligible:cpcv:{pid}:{ev.id}` for
   its downstream emit), `breakout_outcomes` doesn't emit downstream
   events. The "safety" is that the aggregate write is convergent,
   not that we suppress duplicate firings.

## Side effects

Per-pattern (when `len(outcomes) >= 3`, `learning.py:4944-4951`):

- `scan_patterns.win_rate` — overwritten with rounded
  `winners / total` from resolved alerts.
- `scan_patterns.avg_return_pct` — overwritten with mean
  `max_gain_pct`.
- `scan_patterns.trade_count` — recounted from `Trade` table.
- `scan_patterns.updated_at` — bumped.

Per asset_type/tier bucket (when `len(alerts) >= 3`,
`learning.py:4997-5008`):

- `trading_insights.confidence` — EWMA-blended (see above).
- `trading_insights.evidence_count`, `win_count`, `loss_count`,
  `pattern_description`, `last_seen` — overwritten on existing row,
  or a new `TradingInsight` is inserted via `save_insight`.

Plus a `[learning]` log line per pattern and a
`log_learning_event` audit row per bucket.

## Throughput estimate

Same dispatcher cadence math as the backtest_completed memo
(`brain_work_dispatch_batch_size`, default 8 per round; cycle every
25–90 min).

At 30-min cadence:
- 2659 rows / 8 per round = ~333 rounds.
- 333 rounds × 30 min ≈ **166 hours / ~7 days** of dispatcher time.

This is materially longer than `backtest_completed`. Three knobs:

1. **Batch upsize.** Raise the dispatcher's per-round limit
   temporarily.
2. **Wave staging.** Run the backfill in waves (`-MaxRows 500`) so
   the dispatcher can fully drain a wave before the next is flipped.
3. **Compaction.** Because of the EWMA convergence (point 3 above),
   only the first ~10 firings per asset_type/tier bucket carry
   information — the remaining 2649 are no-ops at the convergence
   limit. If wall-clock matters, the operator can run a small wave
   (e.g. 200 rows), verify the stats look sane, and decline to
   replay the remaining 2459 since the additional events change
   nothing once converged.

## Operational guardrails

1. **Run paper_trade_closed + live_trade_closed first.** The recommended
   order in the runbook puts these tiny event types ahead of
   breakout_alert_resolved precisely to surface handler errors before
   the largest set fires.

2. **Watch the breakout-alert pipeline during the run.** If the live
   breakout pipeline is resolving new alerts while the backfill is
   replaying old events, each replay sees a slightly different 180-day
   window. This is not a correctness problem — the aggregate is still
   well-defined — but the per-pattern values will move as new
   alerts resolve. If the operator wants frozen aggregates during the
   backfill, pause the breakout pipeline first.

3. **Watch the `[learning]` log lines.** Each pattern update emits a
   log line at INFO. A sudden flood (more than `batch_size` per
   round) means the dispatcher is over-pulling — kill-switch the
   script (`New-Item scripts/brain-event-backfill-stop.flag`) and
   investigate.

## Rollback

If the backfill produces unexpected state, undo all
`breakout_alert_resolved` flips with:

```sql
UPDATE brain_work_events
SET status = 'done',
    processed_at = CURRENT_TIMESTAMP,
    attempts = 0,
    lease_holder = NULL,
    lease_expires_at = NULL
WHERE domain = 'trading'
  AND event_kind = 'outcome'
  AND event_type = 'breakout_alert_resolved'
  AND payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'
  AND status IN ('pending', 'retry_wait', 'processing');
```

Side effects from already-fired handlers (the per-pattern
ScanPattern + TradingInsight writes) are **not reversed** by this
rollback. Because each firing fully overwrites the aggregate from the
180-day window of BreakoutAlerts, the most reliable way to "undo" the
mutation is to let one more organic
`breakout_alert_resolved` event fire — that re-aggregates the same
window and lands at the same value. There is no separate
"pre-backfill snapshot" to restore from. If a corrupted aggregate is
suspected, snapshot the affected `scan_patterns` and `trading_insights`
rows BEFORE starting the backfill so the operator has a known-good
baseline to compare against.

## Gated event types (DO NOT confuse with this memo)

This memo authorizes replay of `breakout_alert_resolved` only.

**`market_snapshots_batch` is GATED.** Its target handler
(`mine_patterns` via `regime_ledger`) has no event-level dedupe —
the Phase 1b runbook (`docs/runbooks/BRAIN_WORK_EVENT_KIND.md`)
flagged this and Phase 1c (this file) reaffirms it. Do not run the
backfill script against `-EventType market_snapshots_batch` until
the `mine_patterns` inner contract is verified. The script will warn
and pause 5 s before any such run; the runbook section "GATED event
types" describes the verification gate.
