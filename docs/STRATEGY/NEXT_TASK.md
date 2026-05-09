# NEXT_TASK: f-brain-phase2-producer-completion

STATUS: PENDING

## Goal

Complete the Phase 2 brain migration. Operator's diagnostic
confirmed by tonight's audit: the execution layer (event-driven
handlers) migrated cleanly on 2026-05-05, but the production
layer (things that EMIT work-ledger events) didn't. Some
producers were in the legacy `run_learning_cycle` body; when
that cycle was gated off via `CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0`,
the producers stopped firing. **Mining stopped 2026-05-05** (same
day Phase 2 became operational — not coincidence).

The full brief is at
`docs/STRATEGY/QUEUED/f-brain-phase2-producer-completion.md`
— read it first.

## Why now

End-of-day 2026-05-08 audit data:
- `market_snapshots_batch` events: **0 in 4 days** (last fired 5/5)
- New `scan_patterns` rows: **0 in 4 days** (last created 5/5)
- `pattern_eligible_promotion`: **0 in 30 days**

The "narrow funnel" is **not narrow — it's stopped**. Backtest
cadence is healthy (~200-400/day) but it's walking a static pool
of patterns that hasn't changed in a week. Without restoring the
producers, every other improvement is dead air.

## Scope (operator-friendly two-stage)

**Stage 1 (read-only audit, ~30 min)**: map every handler in
`brain_work/handlers/` to its producer. Identify gaps. Output: a
mapping table in the CC report.

**Stage 2 (targeted wiring, ~1-2 hours)**: for each missing
producer, ship a fix. Mining is the load-bearing first fix.
Others may be surfaced and either fixed in this brief OR split
into follow-ups (operator decides after stage 1).

## Why this scope

* **Vs. CC's "just restart mining cron" recommendation**: that
  would fix the symptom but not the architectural mechanism. If
  there are OTHER silent producers besides mining (operator's
  hypothesis suggests this), targeted-mining-fix would leave
  them broken.
* **Vs. broader architectural rebuild Phase 1** (auth liveness):
  multi-week scope; doing tired is dangerous. This brief is
  bounded.
* **Vs. directly expanding the universe**: pointless until the
  producer pipeline can convert candidates to PTRs.
* **Vs. directly loosening the 30-trade gate**: wrong fix —
  Section B of tonight's audit confirmed the gate is correctly
  tight; the trade-accumulation pipeline is upstream-starved.

## The change

Per the brief, two stages:

1. **Stage 1 — handler trigger mapping**:
   For each file in `app/services/trading/brain_work/handlers/`,
   document handler-name, consumed-event, producer-location,
   producer-type (event-driven / scheduled / hybrid / MISSING),
   last-event-seen, status. Surface MISSING producers.

2. **Stage 2 — wire missing producers**:
   - Mining first (the visible bottleneck): restore the producer
     under the new architecture (per-cycle hook in
     `run_brain_work_dispatch_round` preferred).
   - Other MISSING producers (if any): wire in same brief OR
     split to follow-up briefs based on operator's call after
     stage 1.

## Acceptance criteria

1. **Stage 1 mapping table** in the CC report covering every
   handler in `brain_work/handlers/`.
2. **Stage 2 fix for mining** shipped: producer wired, post-deploy
   `market_snapshots_batch` events flow.
3. **Integration test (LIVE PATH, hard requirement)**:
   `tests/test_brain_producer_wiring.py` exercises the full chain
   (trigger → producer → event lands → handler consumes → new
   `scan_patterns` row created). Run ALONE first (lesson from
   tonight's three "tests-pass-but-system-fails" instances).
4. Existing test suite (15+6+12+9 = 42 prior tests) still passes.
5. Live verification post-deploy: brain-worker logs show
   `market_snapshots_batch` events at the chosen cadence; new
   `scan_patterns` rows appear within 24h.
6. CC report at
   `docs/STRATEGY/CC_REPORTS/2026-05-09_f-brain-phase2-producer-completion.md`
   with stage 1 mapping + stage 2 fix details.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/brain_work/handlers/` — read every file.
- `app/services/trading/brain_work/dispatcher.py:run_brain_work_dispatch_round`
  — the per-cycle hook target (already running; tonight's
  pattern-demote wiring is the model).
- `app/services/trading/brain_work/ledger.py:enqueue_work_event` —
  the canonical emit primitive.
- `app/services/trading/learning.py` — read legacy
  `run_learning_cycle` to find what it USED to emit.
- `scripts/scheduler-worker.py` + `app/services/trading_scheduler.py`
  — alternative wiring targets for cron-class periodic work.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** Patterns 1011
  + 1016 and their entry-decision logic must be untouched.
  Autotrader, exit_monitor, bracket_writer must be untouched.
- **DO NOT loosen any gate threshold.** The gate is correctly
  tight; the funnel is upstream-starved, not gate-blocked.
- **DO NOT re-enable** `CHILI_BRAIN_LEGACY_CYCLE_ENABLED`. The
  legacy cycle stays gated off; the fix is to wire producers
  correctly under the NEW architecture.
- **Edit-tool truncation discipline (HARD).**
- **Tests use `_test`-suffixed DB.**
- **No magic numbers** — any new cadence lifts from settings.

## Out of scope

- Universe expansion (separate brief if surfaced).
- Multi-timeframe mining (separate brief if surfaced).
- OOS revalidation as a NEW feature (but in-scope to restore an
  existing OOS-revalidation producer if Stage 1 finds it
  missing).
- The `5-patterns-passed-gate-but-never-emitted` anomaly —
  separate brief (`f-cpcv-gate-emit-anomaly-investigation`).
- Architectural rebuild Phase 1 (auth liveness — multi-week).
- Any change to entry-decision logic, autotrader, exit_monitor,
  or bracket_writer.

## Sequencing

1. Truncation scan.
2. **Stage 1 (read-only)**: produce the mapping table.
3. **Surface to operator**: BEFORE shipping any wiring fix,
   include the mapping table in the CC report draft and surface
   the list of MISSING producers. Operator confirms scope (which
   missing producers to fix in this brief vs spin off).
4. **Stage 2**: ship the wiring fix(es) operator confirmed.
   Mining first.
5. **Integration test FIRST**: write the test, run ALONE, prove
   it fails before fix then passes after fix.
6. Helper-level tests.
7. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate brain-worker scheduler-worker`.
3. Watch brain-worker logs for ~10 min:
   ```
   docker logs -f --tail 0 chili-home-copilot-brain-worker-1 \
     | grep -E 'market_snapshots_batch|new scan_patterns|brain_work_dispatch'
   ```
   Expected: `market_snapshots_batch` events firing at the chosen
   cadence (every 5-15 min, depending on the legacy cycle's
   value).
4. Wait 24h. Run the audit's Section D query — expected: new
   `scan_patterns` rows with `created_at > 2026-05-09`.
5. After 7 days: re-run the eligibility audit's Section A query.
   Expected: `pattern_eligible_promotion` count > 0 in trailing
   7d (assuming any new pattern crosses the 30-PTR floor).

## Rollback plan

`git revert` the commit. Producer wiring is purely additive;
revert removes new emit calls and restores the silent state. No
data loss. Settings flag (whatever name CC chose) disables the
cron without code revert.

## What CC should do if it's unsure

1. **If Stage 1 surfaces multiple missing producers besides
   mining**, surface ALL of them in the CC report draft and
   propose split-vs-bundle. Operator decides after stage 1.
2. **If the legacy mining-emit code is tangled with gate logic
   in `run_learning_cycle`**, surface the entanglement and
   propose how to extract a clean producer function. DO NOT
   modify gate logic to extract.
3. **If the integration test requires real broker / external
   data**, surface the gap and propose a smaller-scope test
   that mocks the data layer but exercises the full event chain.
4. **If wiring the mining producer would require modifying the
   entry-decision side or the autotrader**, STOP — operator's
   "don't break what works" directive forbids it. Surface for
   re-scoping.
