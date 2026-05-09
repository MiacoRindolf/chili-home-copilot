# f-brain-phase2-producer-completion

STATUS: QUEUED
SLUG: brain-phase2-producer-completion
PROPOSED: 2026-05-09
SEVERITY: high (the discovery side of chili's brain has been silent for 4 days; pattern-eligibility funnel produces zero new candidates per day; existing edge is grandfathered, not renewed)

## TL;DR

**Operator's diagnostic, confirmed by tonight's audit:** Phase 2 of
the brain migration (event-driven handlers replacing
`run_learning_cycle`) was incomplete. The execution layer
(handlers) migrated cleanly. The **production layer** (things that
emit `market_snapshots_batch`, etc. into `brain_work_events`)
didn't. The legacy cycle was the implicit producer for some
events; when it was gated off via
`CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0`, those events stopped firing.

Symptom: `market_snapshots_batch` events stopped 2026-05-05 (the
day Phase 2 became operational). Zero new patterns created since
2026-05-02. The "narrow funnel" isn't narrow — it's stopped.

This brief is **two-stage, audit-then-fix**:

1. **Map every handler in `brain_work/handlers/`** to its trigger
   pattern (event-driven / scheduled / hybrid). Identify which
   need scheduled producers, which have organic upstream events.
2. **Wire the missing producers.** For each gap, restore via the
   simplest mechanism (scheduler-cron, per-cycle dispatcher hook,
   or upstream emit). Mining is the immediate priority; other
   producers may be surfaced.

LOW risk to existing system: no gate changes, no entry-decision
changes, no working-trade impact. Restoring producers is purely
additive — failed candidates sit at `lifecycle_stage='candidate'`
and don't fire alerts.

## Why now

End-of-day 2026-05-08 audit (`f-pattern-pipeline-eligibility-audit`,
report committed `05ce2ae`):

| Day | `market_snapshots_batch` events | Pattern creations |
|---|---:|---:|
| 2026-05-02 | 10 | 37 |
| 2026-05-05 | 17 | 0 |
| 2026-05-06 | **0** | 0 |
| 2026-05-07 | **0** | 0 |
| 2026-05-08 | **0** | 0 |
| 2026-05-09 | **0** | 0 |

Backtest cadence is healthy (~200-400/day). The bottleneck is
NOT backtest throughput; it's that the same static pool of
patterns is being walked. No new candidates flow in.

Operator's hypothesis: "When we retired the legacy learning cycle
to event-driven, some are event driven but some still need
scheduled running. The migration wasn't really completed." That's
the right architectural framing.

## Goal

Two stages in one brief:

### Stage 1: handler-trigger mapping (read-only, ~30 min)

Walk every file in `app/services/trading/brain_work/handlers/`
and for each handler:

1. Identify the event type it consumes (e.g., `cpcv_gate.py`
   handles `pattern_eligible_promotion`).
2. Find the upstream **producer**: the code that calls
   `enqueue_work_event(event_type=...)` to put work on the
   ledger.
3. Classify the producer:
   - **Event-driven**: emits in response to an organic upstream
     trigger (e.g., `live_trade_closed` fires when the autotrader
     closes a trade).
   - **Scheduled**: emits on a periodic timer (cron in
     `scheduler-worker` OR per-cycle hook in
     `run_brain_work_dispatch_round`).
   - **Hybrid**: both.
   - **MISSING**: no producer found; if the handler has been
     silent for >24h with no events, it's broken.

4. Output a mapping table in the CC report:

   | Handler | Event consumed | Producer location | Producer type | Last event seen | Status |
   |---|---|---|---|---|---|

### Stage 2: fix the missing producers (targeted, ~1-2 hours)

For each MISSING producer surfaced in stage 1:

1. **Determine the correct trigger pattern** based on the work's
   nature:
   - Mining-class work (discovery, snapshot ingestion) → scheduled
     (e.g., every 5-15 min via scheduler-cron OR per-cycle hook).
   - Trade-driven work (close-events, fill-events) → event-driven
     from existing trade-lifecycle code.
   - Validation-class work (OOS revalidation, regime ledger) →
     scheduled (e.g., daily/hourly cron).

2. **Wire the producer using the simplest mechanism that works**:
   - Prefer per-cycle dispatcher hook (the loop is already running
     every ~75-90s; tonight's pattern-demote-sweep wiring is the
     model).
   - Fall back to scheduler-cron if the cadence doesn't match
     dispatcher rounds.
   - Direct `enqueue_work_event` call from a trigger point only
     if the producer is genuinely event-driven.

3. **Mining-specifically** (the highest-leverage gap):
   - Audit the legacy `run_learning_cycle` code for the mining
     emit path (where it called `enqueue_work_event(event_type=
     'market_snapshots_batch', ...)`).
   - Restore that emit under the new architecture: either as a
     per-cycle hook in `run_brain_work_dispatch_round` (preferred)
     OR as a scheduler-worker cron entry.
   - Verify the producer fires post-deploy via brain-worker logs.

## Acceptance criteria

1. **Stage 1 deliverable**: a mapping table in the CC report with
   one row per handler in `brain_work/handlers/`. Each row carries
   handler-name, consumed-event, producer-location,
   producer-type, last-event-seen, status.
2. **Stage 2 deliverable**: for each MISSING producer surfaced in
   stage 1, a wiring fix is shipped. Mining is the load-bearing
   first fix; others may follow.
3. **Integration test (LIVE PATH, hard requirement)**:
   `tests/test_brain_producer_wiring.py` — for the mining
   producer specifically, seed the upstream trigger
   (e.g., a dispatcher-round invocation), call the trigger,
   assert at least one `market_snapshots_batch` event lands in
   `brain_work_events` AND a new `scan_patterns` row gets
   created with non-zero PTR rows after the downstream backtest
   completes. End-to-end, not helper-level. **Run this test
   ALONE first** before the helpers + commit (lesson from
   tonight's earlier failures).
4. Existing test suite (15 thin-evidence tests + 6 wiring tests +
   12 prefilter tests + 9 resilience tests) still passes.
5. Live verification post-deploy: brain-worker logs show
   `market_snapshots_batch` events flowing again. New
   `scan_patterns` rows appear within 24h of restart.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-brain-phase2-producer-completion.md`
   with the stage 1 mapping + stage 2 fix details.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/brain_work/handlers/` — read every file
  to identify event-type / producer link.
- `app/services/trading/brain_work/dispatcher.py` —
  `run_brain_work_dispatch_round` is the per-cycle hook target
  (already-running loop; tonight's pattern-demote wiring is the
  proven model).
- `app/services/trading/brain_work/ledger.py` —
  `enqueue_work_event` is the canonical emit primitive.
- `app/services/trading/learning.py` — read the legacy
  `run_learning_cycle` code (gated off but presumably still in
  the file) to identify what it USED to emit.
- `scripts/scheduler-worker.py` — alternative wiring target for
  cron-class periodic work.
- `app/services/trading_scheduler.py` — TradingScheduler class
  if the cron is registered there.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** The currently-
  promoted patterns (1011, 1016) and their entry-decision logic
  must be untouched. The autotrader, exit_monitor, and
  bracket_writer must be untouched.
- **DO NOT loosen any gate threshold.** The gate is correctly
  tight; the funnel is upstream-starved, not gate-blocked.
- **DO NOT re-enable** `CHILI_BRAIN_LEGACY_CYCLE_ENABLED`. The
  legacy cycle is gated off intentionally; the fix is to wire the
  producers correctly under the new architecture.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**
- **No magic numbers** — any new cadence (e.g., mining cron
  interval) lifts from settings (`CHILI_BRAIN_MINING_INTERVAL_SECS`
  default 300 or whatever value the legacy cycle used).

## Out of scope

- Universe expansion (separate brief if Stage 1 surfaces it).
- Multi-timeframe mining (separate brief if surfaced).
- OOS revalidation wiring as a NEW feature (but if Stage 1
  finds OOS revalidation has a missing producer, it's in scope
  to restore as a scheduled cron).
- The `5-patterns-passed-gate-but-never-emitted` anomaly from
  Section A of tonight's audit (separate brief:
  `f-cpcv-gate-emit-anomaly-investigation`).
- Architectural rebuild Phase 1 (auth liveness — separate
  multi-week initiative).
- Any change to entry-decision logic, autotrader, exit_monitor,
  or bracket_writer.

## Sequencing

1. Truncation scan on `brain_work/handlers/`,
   `brain_work/dispatcher.py`, `brain_work/ledger.py`,
   `learning.py` (legacy cycle code).
2. **Stage 1 (audit, read-only)**: produce the handler mapping
   table in the CC report. Identify the MISSING producers.
3. Surface to operator the list of missing producers BEFORE
   shipping any code. Operator confirms which to fix in this
   brief vs spin off as separate briefs.
4. **Stage 2 (targeted wiring)**: ship one wiring fix per
   confirmed-missing producer. Mining is the load-bearing first
   fix.
5. **Integration test FIRST**: write the mining-producer
   integration test, run it ALONE, prove it fails before fix
   then passes after fix. This is the discipline from tonight.
6. Helper-level tests for each wiring fix.
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch brain-worker logs for the missing producer events:
   ```
   docker logs -f --tail 0 chili-home-copilot-brain-worker-1 \
     | grep -E 'market_snapshots_batch|new scan_patterns'
   ```
   Expected: events firing at the brief's chosen cadence
   (~5-15 min for mining).
4. Wait 24h. Confirm new `scan_patterns` rows appear with
   `created_at > 2026-05-09`. Run the audit's section D query
   to verify cadence.
5. After 7 days: re-run the eligibility audit's section A query.
   Expected: `pattern_eligible_promotion` count > 0 in trailing
   7d (assuming any new pattern crosses the 30-PTR floor).

## Rollback plan

`git revert` the commit. The producer wiring is purely additive —
revert removes the new emit calls and restores the silent state.
No data loss; existing patterns remain at their current
lifecycle stage. Settings flag
`CHILI_BRAIN_MINING_INTERVAL_SECS=0` (or equivalent) disables
the cron without code revert.

## Open questions

1. **Where is the legacy `run_learning_cycle` mining-emit code
   actually located?** Stage 1's audit will find it. If it's in
   `learning.py` and the gating is just an `if CHILI_BRAIN_LEGACY_CYCLE_ENABLED:`
   check around the entire body, the wiring fix is to extract
   ONLY the mining-emit portion under a separate flag/hook.
2. **Are there OTHER silent producers besides mining?** Stage 1
   will tell. If so, this brief either expands to fix them all
   OR splits each into a follow-up brief — operator decides
   after stage 1.
3. **Cadence for the new mining producer**: every dispatcher
   round (~75-90s) might be too aggressive. Probably 5-15 min
   matches the legacy cycle's intent. The exact value should
   come from settings, lifted from whatever the legacy cycle
   used.

## What CC should do if it's unsure

1. **If stage 1 surfaces multiple missing producers**, surface
   ALL of them in the CC report and propose splitting the
   wiring fixes by priority. Mining is highest-priority; others
   may defer.
2. **If the legacy `run_learning_cycle` mining-emit code is
   harder to extract than expected** (e.g., it's tangled with
   gate logic), surface the choice: extract a clean producer
   function vs add a fresh producer call. Operator picks.
3. **If the integration test setup requires real broker calls
   or external data fetches** (which don't work cleanly in
   chili_test), surface the gap and propose a smaller-scope
   integration test that mocks the data-fetch layer but
   exercises the full event-emit → handler-consume → DB-write
   chain.
4. **If the mining producer can't be wired without touching the
   gate logic** (suggesting the gate and producer are tangled),
   STOP and surface — operator's "don't break what works"
   directive forbids gate changes in this brief.
