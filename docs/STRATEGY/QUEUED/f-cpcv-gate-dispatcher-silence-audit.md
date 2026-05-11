# f-cpcv-gate-dispatcher-silence-audit (Phase 1a of adaptive-promotion-architecture)

> **Type:** Read-only audit (NO code changes)
> **Parent brief:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`
> **Prior phase:** `docs/AUDITS/2026-05-11_cpcv_gate_coverage.md` (Phase 0)
> **Goal:** Find why `run_brain_work_dispatch_round` has logged zero
> lines in full brain-worker history, AND identify the writer that is
> marking 205 `backtest_completed` events/24h as `done` without invoking
> the cpcv_gate handler.
> **Trust budget:** none. This phase touches no DB writes, no `app/`
> code, no restarts. Read code + read DB + read logs.

## Why this phase exists

Phase 0 audit established:
- 50/50 sampled candidate patterns are stuck (52% never received a
  `backtest_completed` event; 48% received one but the handler never
  logged a verdict).
- Full brain-worker container history shows **zero**
  `[brain_work:dispatch]` and **zero** `[brain_work:cpcv_gate]` log
  lines (vs. handler_verify OK at startup).
- Yet `brain_work_events` shows 205 events with `status='done'` in 24h.
- Some writer is marking events done without going through
  `run_brain_work_dispatch_round`. That writer is unidentified.

Phase 1b (synthetic-event backfill) is unsafe to ship until we know
*why* the dispatcher is silent. If we enqueue 275 events and nothing
drains them, we just inflate the backlog. Worse: if the rogue
done-writer marks the new events done without invoking the handler,
Phase 1b becomes a no-op that *looks* successful.

## Hypotheses to test (in order of likelihood)

**H1: `run_brain_work_dispatch_round` is not running at all.** A
worker that should call it hasn't been calling it. Candidates:
brain-worker main loop, scheduler-worker via APScheduler, fast-data-worker.
Check container start-up bootstrap for the call site.

**H2: The function is running but its logger is filtered.** Some log
config (LOG_LEVEL, logger name override) is silencing the
`[brain_work:dispatch]` prefix but the loop is firing. Check
`logging.getLogger("app.services.trading.brain_work.dispatcher")` and
the project root logging config.

**H3: `brain_work_ledger_enabled()` returns False.** Feature flag is
off; the dispatcher exits early without logging. Check the flag's
read site + the trading_settings row.

**H4: Legacy `run_learning_cycle` is the rogue done-writer.** Despite
the gate-off flag (`CHILI_BRAIN_LEGACY_CYCLE_ENABLED=false`), some
cycle step is still writing `status='done'` directly into
`brain_work_events`. Check `learning.py` for `update(BrainWorkEvent)`
calls.

**H5: `backtest_queue_worker.py` is marking its own events done.**
The backtest-queue path may emit `backtest_completed` AND mark it done
in the same transaction, bypassing the dispatcher. Check the emitter
call chain.

**H6: A different handler is consuming the events** under a different
log prefix (or no prefix). Check `brain_work/handlers/__init__.py` for
registered handler names + the dispatcher's iteration logic.

## Deliverables

### D1. `scripts/audit-dispatcher-silence.ps1`

Idempotent, read-only PowerShell script that:

1. **Identifies the running brain-worker process tree** — `docker top
   brain-worker-1` + `ps -ef` inside the container, grep for
   `run_brain_work_dispatch_round` in stack traces if possible
   (`py-spy dump` is overkill; pgrep is fine).

2. **Counts dispatcher invocations by source.** Query:
   ```sql
   SELECT
     COALESCE(payload->>'source', 'unknown') AS source,
     status, COUNT(*) AS n,
     MIN(created_at) AS first_ts,
     MAX(updated_at) AS last_done_ts
   FROM brain_work_events
    WHERE event_type='backtest_completed'
      AND created_at > NOW() - INTERVAL '24 hours'
    GROUP BY 1, 2
    ORDER BY n DESC;
   ```
   Same query for `event_type='pattern_eligible_promotion'` and
   `event_type='backtest_requested'`.

3. **Reads the dispatcher loop call site.** `grep -nr
   "run_brain_work_dispatch_round" app/` → list every file that
   invokes the loop, mark active vs commented-out vs scheduled.

4. **Inspects the feature flag.** Query `trading_settings` for any
   key matching `%brain%dispatch%` or `%brain%ledger%`. Read the
   flag's check site in `app/services/trading/brain_work/`.

5. **Greps logs across ALL workers.** Per-container 24h counts:
   ```
   for c in brain-worker scheduler-worker autotrader-worker \
            broker-sync-worker fast-data-worker chili; do
     docker logs --since 24h chili-home-copilot-$c-1 2>&1 \
       | grep -c "brain_work:dispatch" || true
   done
   ```
   Same with `brain_work:cpcv_gate`, `brain_work:promote`,
   `brain_work:demote`, `brain_work:mine`, `brain_work:pattern_stats`,
   `brain_work:regime_ledger`. Each handler should produce *some* logs
   if the dispatcher is calling it.

6. **Finds the rogue done-writer.** PostgreSQL `pg_stat_user_functions`
   + `pg_stat_activity` won't directly attribute writes, but we can:
   - Grep `app/` for SQL strings matching
     `UPDATE brain_work_events SET status` or
     `brain_work_events.status = "done"`.
   - Grep for the ORM analog: `.status = "done"` near a
     `BrainWorkEvent` query.
   - List all call sites; classify as "dispatcher-internal" vs
     "external writer".

7. **Spot-check one recently-done event** — pick the most recent
   `status='done'` row, print its `payload`, `parent_event_id`, and
   the file ownership of the writer if grep narrowed it.

Output: `scripts/audit-dispatcher-silence-out.txt`.

### D2. `docs/AUDITS/2026-05-11_dispatcher_silence.md`

One-page memo with:
- For each of H1–H6, status (confirmed / ruled out / inconclusive) +
  evidence excerpt.
- Identified rogue done-writer (file:line + brief description of what
  it's doing).
- Recommendation for Phase 1b's safety properties: does the
  synthetic-event backfill need to go through a different code path?
  Are there events the dispatcher *would* drain that the rogue writer
  hasn't touched yet (a pure dispatcher-restoration test)?
- Concrete handoff to Phase 1b: what to enqueue, who will drain it,
  expected throughput.

### D3. `docs/STRATEGY/CC_REPORTS/2026-05-11_cpcv-gate-dispatcher-silence-audit.md`

Standard CC_REPORT.

## Hard constraints

- **READ-ONLY.** No DB writes. No `app/` code changes. No restarts.
  No new migrations / tables / columns. No env edits.
- All `psql` calls SELECT-only.
- No `docker exec python -c` that mutates state — readonly Python
  imports + grep only.
- No changes to `dispatcher.py`, any handler, or
  `backtest_queue_worker.py` — read them, don't edit them.
- Memo D2 must name the rogue done-writer by file:line, not by
  category. If we can't pin it to a file, mark "inconclusive" and
  recommend additional Phase 1a' probe.

## Success criteria

- D1 + D2 + D3 committed
- `scripts/audit-dispatcher-silence-out.txt` committed (raw run output)
- All 6 hypotheses have a status verdict (confirmed / ruled out /
  inconclusive with reason)
- Concrete recommendation for Phase 1b safety / sequencing

## Approved next step after CC_REPORT lands

Cowork will read the memo, write Phase 1b
(`f-cpcv-gate-event-backfill.md`) calibrated to the dispatcher's
actual state, and surface to operator. If the memo says the
dispatcher is dead AND the rogue done-writer is the autotrader's path
(would be very bad), Phase 1b morphs into "fix the dispatcher first."
