# NEXT_TASK: f-handler-pattern-stats

STATUS: DONE

## Goal

Wire `update_pattern_stats_from_closed_trades`
(`app/services/trading/learning.py:4798`) into a new event handler
`app/services/trading/brain_work/handlers/pattern_stats.py` that
subscribes to trade-close events. After this ships:

- Every paper / live / broker trade close fires the canonical-aware
  evidence recompute for that user's affected pattern.
- `pattern_evidence_corrections` audit-table populates per close
  (one row per pattern processed).
- The realized-EV gate auto-demotes patterns whose corrected stats
  fail the gate (existing behaviour, no new code).
- Today's f-evidence-canonical-writer fix transitions from
  "shipped but inert" to **operationally real**.

This is the **smallest possible brief that completes the
canonical-writer chain**. The function it wraps is already
production-ready (mig 228, 14/14 tests passing). The handler is a
thin event-driven shim.

## Why now

Today's `f-kill-legacy-learning-cycle` brief gated off the only path
that called `update_pattern_stats_from_closed_trades` (it was step
11 of `run_learning_cycle`, which is now disabled). Without this
handler, the canonical-writer fix is dead code:

- The function is correct, tested, deployed
- The audit table exists and is empty
- No caller invokes it

The post-deploy smoke from `f-kill-legacy-learning-cycle` confirmed
brain-worker stability (488 MB memory, zero connection drops, zero
`learning_cycle_end` events) but ALSO surfaced that
`brain_work_events` had zero rows in 30 min — partly because nothing
emitted close events during that window, and partly because no
handler was waiting for them.

This brief closes the loop. After it ships:
- Closed trades emit events (already wired via
  `paper_trading.py:240 → execution_hooks.py:30 → emitters.py:107`)
- Handler claims the event, calls the canonical-writer function,
  one audit row written
- Pattern evidence becomes self-correcting per-close, in the
  intended event-driven cadence

## Scope boundary

**In scope:**
- New module `app/services/trading/brain_work/handlers/pattern_stats.py`.
- Wire the handler into `dispatcher.py`'s dispatch loop alongside the
  existing 5 handlers.
- Pre-execution audit: confirm `paper_trade_closed` /
  `live_trade_closed` / `broker_fill_closed` events ARE firing in
  recent history. If audit shows zero events in last 24h despite
  closed trades existing, surface the wiring break and STOP — do not
  ship a handler that depends on emitter that's broken.
- Tests for the handler logic.
- Smoke verification: synthetic close fires the handler, audit row
  appears.

**Out of scope:**
- Modifying `update_pattern_stats_from_closed_trades` itself. It's
  already production-ready.
- Modifying the canonical evaluator
  (`exit_evaluator.py`).
- Modifying any other handler. The 5 existing Phase 2 handlers stay
  as they are.
- Modifying the realized-EV gate or promotion gate. Inputs come from
  this handler; auto-demote falls out for free.
- Re-enabling `run_learning_cycle`. Stays gated off via
  `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0`.
- Other cycle-replacement handlers
  (`f-handler-breakout-outcomes`, `f-handler-validate-evolve`, etc.).
  Separate briefs from `PHASE2_HANDLER_BACKLOG.md`.
- Position-side timeframe column (Trade/PaperTrade.timeframe).
  Existing `scan_pattern_id → ScanPattern.timeframe` lookup
  inherited from f-time-decay-unit-fix.
- LLM-context (`position_plan_generator`) pattern-evidence path.
  Reads ScanPattern fields; benefits transparently once handler
  fires.

## Brain integration / source material

- `app/services/trading/brain_work/handlers/mine.py` — model for
  the handler shape (FIX 36, 2026-04-29). Read first.
- `app/services/trading/brain_work/handlers/demote.py` and
  `regime_ledger.py` — handlers that ALREADY subscribe to
  `paper/live/broker_*_closed` events. Same subscription pattern;
  pattern-stats is a third subscriber to the same events.
- `app/services/trading/brain_work/dispatcher.py:272-321` — the
  dispatch loop. New event-type dispatch branch lands here.
- `app/services/trading/brain_work/emitters.py:94-130` —
  `emit_paper_trade_closed_outcome` etc. Emitter exists. Verified.
- `app/services/trading/brain_work/execution_hooks.py:27-50` —
  `on_paper_trade_closed` (the function paper_trading.py calls
  inline). Calls the emitter. Verified.
- `app/services/trading/paper_trading.py:240-245` —
  `_paper_close_ledger` calls `on_paper_trade_closed` from inside
  the close transaction. Verified.
- `app/services/trading/learning.py:4798` —
  `update_pattern_stats_from_closed_trades`. Function the handler
  calls. Already production-ready (mig 228, 14 tests passing).
- `app/config.py` — handler batch-size config pattern. Add
  `brain_work_pattern_stats_batch_size` alongside the other
  `brain_work_*_batch_size` settings.
- `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` — top-of-list entry is
  this handler. Mark it shipped after this brief lands.

## Path

### Step 0 — Pre-execution audit (DO BEFORE ANY CODE CHANGE)

The smoke window from `f-kill-legacy-learning-cycle` showed zero
`brain_work_events` activity. Two possibilities:

1. Quiet 30-min window with no paper / live / broker trade closes.
2. Wiring break — close events aren't being emitted even when trades
   close.

Confirm which. Run:

```sql
-- Recent paper closes in last 24h
SELECT COUNT(*) AS paper_closes_24h
FROM trading_paper_trades
WHERE status = 'closed' AND exit_date >= NOW() - INTERVAL '24 hours';

-- Recent live closes in last 24h
SELECT COUNT(*) AS live_closes_24h
FROM trading_trades
WHERE status = 'closed' AND exit_date >= NOW() - INTERVAL '24 hours';

-- Close events in brain_work_events in last 24h
SELECT event_type, COUNT(*) AS n
FROM brain_work_events
WHERE event_type IN ('paper_trade_closed', 'live_trade_closed', 'broker_fill_closed')
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY event_type;
```

**Decision tree:**
- **If close counts > 0 AND event counts > 0 (in roughly equal
  ratios)**: emitter is working, the smoke window was just quiet.
  **Proceed to Step 1.**
- **If close counts > 0 AND event counts == 0**: emitter wiring is
  broken. Trace `paper_trading.py:240 → execution_hooks.py:30 →
  emitters.py:107` to find the break. **Surface in CC report and
  STOP — fix the emitter before shipping the handler.**
- **If close counts == 0**: no trades have closed in 24h. Brain has
  been hibernating. Confirm with:
  ```sql
  SELECT COUNT(*) FROM trading_paper_trades WHERE status = 'open';
  ```
  If open positions exist but none have closed, that's a separate
  question (likely time-decay or stop-hit not firing — but the new
  time-decay-unit-fix means time_decay should fire for non-1d
  positions soon). **Proceed to Step 1 anyway — the handler is
  correct regardless of current event volume.**

Surface findings in the CC report's "Pre-execution audit" section.

### Step 1 — New handler module

Create `app/services/trading/brain_work/handlers/pattern_stats.py`
following the shape of `demote.py` (which subscribes to the same
events):

```python
"""Phase 2 handler: pattern-evidence recompute on trade close.

Subscribes to ``paper_trade_closed`` / ``live_trade_closed`` /
``broker_fill_closed``. For each event, calls
``update_pattern_stats_from_closed_trades`` for the closed trade's
user. The function is the canonical-aware writer (mig 228) — it
re-derives ``ScanPattern.{win_rate, avg_return_pct, trade_count}``
using counterfactual exit prices for trades that held past their
intended ``max_bars``.

Design rules:
- Per-event, NOT per-trade-affected. The function buckets all of a
  user's recent closed trades by pattern internally, so calling it
  once per close-event handles all patterns the close affected.
- Idempotent: the function writes a ``correction_reason='no_change'``
  audit row when the recompute matches existing stats. Repeated
  invocations don't produce drift.
- Failures swallowed at the handler boundary so a broken pattern's
  recompute can't poison subsequent events.
- Coverage gate (>50% counterfactual-unavailable) is enforced inside
  the function; handler doesn't second-guess.
"""
from __future__ import annotations
import logging
from typing import Any
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def handle_paper_trade_closed(db: Session, ev: Any, user_id: int | None) -> None:
    """Handler entry for paper_trade_closed events."""
    _run_pattern_stats_recompute(db, ev, user_id, source="paper")


def handle_live_trade_closed(db: Session, ev: Any, user_id: int | None) -> None:
    """Handler entry for live_trade_closed events."""
    _run_pattern_stats_recompute(db, ev, user_id, source="live")


def handle_broker_fill_closed(db: Session, ev: Any, user_id: int | None) -> None:
    """Handler entry for broker_fill_closed events."""
    _run_pattern_stats_recompute(db, ev, user_id, source="broker")


def _run_pattern_stats_recompute(
    db: Session, ev: Any, user_id: int | None, *, source: str,
) -> None:
    from ...learning import update_pattern_stats_from_closed_trades
    try:
        result = update_pattern_stats_from_closed_trades(db, user_id)
        logger.info(
            "[handler:pattern_stats] source=%s event_id=%s user_id=%s "
            "patterns_updated=%d cycle_run_id=%s",
            source, getattr(ev, "id", None), user_id,
            int(result.get("patterns_updated", 0)),
            result.get("cycle_run_id"),
        )
    except Exception as e:
        logger.exception(
            "[handler:pattern_stats] source=%s event_id=%s failed: %s",
            source, getattr(ev, "id", None), e,
        )
```

The handler is intentionally thin. All the work is in
`update_pattern_stats_from_closed_trades`. Three entry points
because three different event types subscribe to the same logic.

### Step 2 — Wire into dispatcher

In `app/services/trading/brain_work/dispatcher.py`, find the
`_dispatch_limits` dict (around line 272 per saved memory) and the
event-type dispatch branches. **The three close events
(`paper_trade_closed`, `live_trade_closed`, `broker_fill_closed`)
are ALREADY dispatched** to `demote.py` and `regime_ledger.py`.
This brief adds `pattern_stats` as a THIRD subscriber to each.

Pattern: each event-type's dispatch branch already calls multiple
handlers in sequence. Add a third call:

```python
# In the paper_trade_closed branch:
from .handlers import demote, regime_ledger, pattern_stats  # add pattern_stats

# Existing handler calls stay; add:
pattern_stats.handle_paper_trade_closed(db, ev, user_id)

# Same pattern for live_trade_closed and broker_fill_closed branches.
```

Read `dispatcher.py:272-321` to find the exact insertion points.

### Step 3 — Config setting

In `app/config.py`, add the batch-size setting alongside the existing
`brain_work_*_batch_size` settings:

```python
brain_work_pattern_stats_batch_size: int = 4
```

Default `4` because the recompute function does heavy work
(potentially fetching OHLCV for counterfactual exits per overheld
trade). Lower batch size keeps the dispatch round responsive.

### Step 4 — Tests

`tests/test_handler_pattern_stats.py`:

1. ✅ `handle_paper_trade_closed` calls
   `update_pattern_stats_from_closed_trades` with the event's
   user_id.
2. ✅ `handle_live_trade_closed` same pattern.
3. ✅ `handle_broker_fill_closed` same pattern.
4. ✅ Handler swallows exceptions (a deliberately-broken stub
   `update_pattern_stats_from_closed_trades` raises; handler logs +
   continues).
5. ✅ Integration: synthetic paper close → `_paper_close_ledger`
   call → emitter → event in queue → dispatch → handler →
   `pattern_evidence_corrections` row appears.
6. ✅ Idempotence: calling the handler twice on the same event
   produces matching audit rows (second invocation results in
   `correction_reason='no_change'` per the function's existing
   idempotence test).
7. ✅ Existing exit-evaluator + parity tests still pass (regression
   guard).

### Step 5 — Smoke verification (deferred to deploy)

After deploy:

1. Restart brain-worker:
   `docker compose restart brain-worker`
2. Wait for next paper trade close (or trigger one manually for
   smoke) and watch logs:
   ```powershell
   docker compose logs brain-worker --since 5m |
     Select-String "handler:pattern_stats|paper_trade_closed"
   ```
   Expected: a `[handler:pattern_stats] source=paper event_id=...
   patterns_updated=N` line per close.
3. Confirm audit row landed:
   ```sql
   SELECT correction_reason, COUNT(*), MAX(created_at) AS most_recent
   FROM pattern_evidence_corrections
   WHERE created_at >= NOW() - INTERVAL '10 minutes'
   GROUP BY correction_reason ORDER BY MAX(created_at) DESC;
   ```
   Expected: `first_run_backfill` rows on the first invocation per
   pattern, then `periodic_recompute` or `no_change` on subsequent.
4. Confirm `brain_work_events` shows the close event was
   processed:
   ```sql
   SELECT event_type, status, COUNT(*) FROM brain_work_events
   WHERE event_type LIKE '%trade_closed%'
     AND updated_at >= NOW() - INTERVAL '10 minutes'
   GROUP BY event_type, status;
   ```
   Expected: `status='done'` rows on the close events.
5. Confirm realized-EV gate still functioning (one-shot probe to
   verify auto-demote: pick a pattern in the audit table whose
   `after_avg_return_pct < 0` and verify `lifecycle_stage` flipped):
   ```sql
   SELECT sp.id, sp.lifecycle_stage, sp.win_rate, sp.avg_return_pct
   FROM scan_patterns sp
   WHERE sp.id IN (
       SELECT scan_pattern_id FROM pattern_evidence_corrections
       WHERE after_avg_return_pct < 0
         AND created_at >= NOW() - INTERVAL '10 minutes'
   );
   ```
   Expected: any pattern with negative after_avg_return_pct should
   have `lifecycle_stage='challenged'` or `'demoted'`.

## Constraints / do not touch

- **Default mode stays paper.** No live placement enable.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **Do not modify `update_pattern_stats_from_closed_trades`.** It's
  the canonical-aware writer and stays as-is.
- **Do not modify any of the 5 existing Phase 2 handlers.** This
  brief adds a 6th alongside them.
- **Do not re-enable the legacy cycle.** It stays gated off via
  `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0`.
- **Do not modify the realized-EV gate or promotion gate.** Auto-
  demote falls out for free.
- **Do not modify the dispatcher's batch-size logic** beyond
  adding the new key in `_dispatch_limits`.
- **No threshold tuning.** Default batch size of 4 is documented
  inline; operator overrides via env var if needed.
- **No migrations.** Schema change isn't required.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Pre-execution audit (Step 0) is REQUIRED.** If close-event
  emitters are broken, this brief STOPS and surfaces the break.
  Do not ship a handler whose subscription is never delivered.

## Out of scope

- Backtest-derived evidence correction. Different surface, separate
  brief gated on `f-exit-parity-metric-v2` cutover.
- Per-trade audit granularity. Pattern-level audit is sufficient.
- Other handlers from `PHASE2_HANDLER_BACKLOG.md`.
- Notifying the operator on per-pattern demotion. Audit table is
  the alert surface.
- `position_plan_generator.py` LLM-context path.
- `f-cron-stale-promoted` (sweep-mode demote gap). Separate brief.

## Success criteria

1. **`pattern_stats.py` exists** with the three `handle_*` entry
   points and the shared `_run_pattern_stats_recompute` helper.
2. **Dispatcher wires the handler** to all three close-event types
   alongside `demote.py` and `regime_ledger.py`.
3. **`brain_work_pattern_stats_batch_size`** config setting added
   with documented default.
4. **All 7 new tests pass + existing parity tests still pass**
   against `chili_test`.
5. **Pre-execution audit step results documented in CC report**
   — confirms emitter wiring is firing OR surfaces the break.
6. **Smoke verification passes** (or honestly notes "no closes
   during deploy window — verify on next close" if applicable).
7. **`PHASE2_HANDLER_BACKLOG.md` updated**: mark the
   `update_pattern_stats_from_closed_trades` row as ✅ shipped,
   reference this brief.
8. **CC report** at
   `docs/STRATEGY/CC_REPORTS/<date>_f-handler-pattern-stats.md`
   per PROTOCOL format.

## Rollback plan

- **Code rollback**: `git revert` the implementation commit. The
  dispatcher reverts to dispatching only to `demote.py` and
  `regime_ledger.py`. No handler subscription on
  `pattern_stats`. `pattern_evidence_corrections` table stays as
  it was; existing rows preserved.
- **No data rollback** — the function writes audit rows via
  existing schema; no schema changes in this brief.
- **No live-broker rollback** — task is read-only on closed trades,
  no broker calls.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Pre-execution audit results** — what do `paper_closes_24h`,
   `live_closes_24h`, and `event_counts` look like? If counts
   diverge (closes > events), surface as a wiring break that
   needs separate investigation BEFORE shipping the handler.

2. **The 10-minute idle-in-tx leaker observed in the cycle-kill
   smoke** — pid 60252 holding `SELECT scan_patterns ...` for 624s.
   That wasn't from the cycle (cycle gated off). Could the new
   handler exacerbate it (more queries against `scan_patterns`)?
   Surface if post-deploy data shows the count rising. The
   `update_pattern_stats_from_closed_trades` function does query
   `scan_patterns` to read `timeframe` per overheld trade — if a
   batch processes many overheld trades, that's many short reads.
   Each should be quick and committed; the leaker is something
   else.

3. **Backtest_completed events** — section 5b of the cycle-kill
   smoke showed 784 backtest-source parity rows in 5 min, but
   `brain_work_events` had zero `backtest_completed` events. That's
   a SEPARATE wiring concern (FIX 34's independent loop bypasses
   the event path) and out of scope here, but worth flagging:
   `cpcv_gate.py` (handler #2) subscribes to `backtest_completed`,
   so if those events aren't firing, CPCV gate isn't running.
   Surface for a future `f-fix-backtest-completed-emitter` brief.

4. **First-cycle backfill timing** — the first time this handler
   fires post-deploy, the closed trade's user has all their
   recent (180-day) closed trades evaluated. That could be slow
   if the user has hundreds of closed trades. Mitigation: the
   batch_size of 4 limits concurrent dispatch; each handler call
   is its own short transaction. If first-fire takes >30s in
   practice, surface and consider lowering batch_size further or
   adding pagination inside the function.

5. **Demote and pattern-stats both fire on the same event** —
   `demote.py` re-evaluates promotion lifecycle on close; this
   handler corrects the evidence inputs the gate reads. Order
   matters: if pattern-stats fires FIRST (correcting the
   evidence), then demote runs the gate against the corrected
   evidence. If demote fires first, it reads stale evidence.
   The dispatcher's branch order determines this. **Verify the
   intended order in the dispatch branch and document explicitly.**
   Recommended: pattern-stats BEFORE demote, so demote sees
   corrected stats. If the dispatcher dispatches concurrently
   instead of sequentially, this becomes a synchronization
   question — surface honestly.
