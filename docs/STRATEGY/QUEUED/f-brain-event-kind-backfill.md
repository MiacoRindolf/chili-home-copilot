# f-brain-event-kind-backfill (Phase 1c of adaptive-promotion-architecture)

> **Type:** One-shot controlled backfill (no app/ changes; SQL + script)
> **Hard prereq:** Phase 1b (`f-brain-event-kind-unify.md`) shipped, flag
> `chili_brain_outcome_claimable_enabled=True` in production, and 24h of
> clean handler activity observed.
> **Goal:** Resurrect a scoped subset of the ~4,000 historical
> outcome/done orphans so handlers process them, with operator-controlled
> rate limits and event-type scoping to prevent thundering-herd effects.

## Why this needs its own phase

Phase 1a found 4,033 historical outcome/done rows across 7 event types
that have never been processed by their target handlers:

| event_type | done rows | target handler |
|---|---|---|
| `breakout_alert_resolved` | 2659 | `breakout_outcomes` |
| `backtest_completed` | 1055 | `cpcv_gate` |
| `market_snapshots_batch` | 179 | `regime_ledger` |
| `broker_fill_closed` | 131 | `execution_robustness` |
| `live_trade_closed` | 4 | `live_drift` |
| `paper_trade_closed` | 1 | `live_drift` |
| `pattern_eligible_promotion` | 0 | `promote` (none yet because cpcv_gate hasn't fired) |

Flipping the Phase 1b flag to True does NOT touch these rows
(`status='done'` keeps them ineligible to claim). New traffic flows
correctly through the handlers, but the historical evidence sits
unprocessed. Phase 1c is the operator-controlled mechanism to bring
that evidence forward.

This needs its own phase because the impact is operator-significant:

- Processing 2,659 historical `breakout_alert_resolved` events all at
  once could write substantial pattern-state changes if the
  `breakout_outcomes` handler isn't designed for batch replay.
- Processing 1,055 `backtest_completed` events all at once could
  produce 1,055 cpcv_gate decisions in a single dispatcher window —
  even at `batch_size=8`, that's 130 rounds.
- The 131 `broker_fill_closed` rows correspond to real broker fills
  that already settled. `execution_robustness` handler running on
  them now produces post-hoc audit data — useful but not urgent.

Operator picks which event types to backfill, in what order, at what
rate.

## Scope

### Deliverable D1: `scripts/brain-event-backfill.ps1`

PowerShell helper that:

1. Reads command-line args: `-EventType <name> -BatchSize <int>
   -MaxRows <int> -DryRun:$true|$false`.
2. Selects up to `MaxRows` historical rows matching `event_kind='outcome'
   AND event_type=<EventType> AND status='done' AND processed_at IS NOT
   NULL` (i.e., already-terminal outcomes from the legacy producer
   path).
3. If `-DryRun`, prints the row IDs that WOULD be flipped + the count.
4. If not dry-run, in batches of `BatchSize`:
   - `UPDATE brain_work_events SET status='pending', processed_at=NULL,
     attempts=0, lease_holder=NULL WHERE id IN (...)`. Writes a marker
     `payload->>'backfill_source' = 'phase_1c_2026_05_NN'` for audit.
   - Waits 30 seconds between batches (lets the dispatcher drain;
     prevents lock contention).
5. Tracks progress in `scripts/brain-event-backfill-progress.log`.
6. Honors a kill switch: if
   `scripts/brain-event-backfill-stop.flag` appears, halts after
   current batch.

### Deliverable D2: per-event-type pre-flight memo

For each event_type the operator intends to backfill, a 1-paragraph
analysis in `docs/AUDITS/2026-05-11_backfill_safety_<event_type>.md`:

- Idempotency: does the target handler do the right thing if the same
  event_id is processed twice? (Pulled from Phase 1b's
  test_brain_work_handler_idempotency results.)
- Side effects: what tables does the handler mutate? (e.g.,
  cpcv_gate writes to `scan_patterns.cpcv_*` columns; breakout_outcomes
  writes to `breakout_alerts` + `pattern_evidence_*`.)
- Throughput estimate: at `batch_size=8` and ~25-min dispatcher
  cadence, how long to drain N rows?
- Rollback: if backfill produces unexpected state, what's the undo?

### Deliverable D3: `docs/runbooks/BRAIN_EVENT_BACKFILL.md`

Operator runbook covering:

- The unified queue model (cross-link to Phase 1b's runbook).
- How to use the backfill script.
- Per-event-type recommended order (suggested):
  1. `paper_trade_closed` (1 row, smallest blast radius — verifies the
     pipeline end-to-end with negligible state change).
  2. `live_trade_closed` (4 rows, same handler as #1, builds
     confidence).
  3. `market_snapshots_batch` (179 rows, populates regime_ledger
     baseline — useful for the adaptive gate Phase 2).
  4. `broker_fill_closed` (131 rows, post-hoc execution audit).
  5. `backtest_completed` (1055 rows, the original Phase 1 target —
     this is what fixes the cpcv_n_paths drought).
  6. `breakout_alert_resolved` (2659 rows, largest; do last after
     others are clean).
- How to read progress via the log + `brain_work_events` snapshot
  query.
- Kill switch usage.

### Deliverable D4: CC_REPORT
`docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-backfill.md`.

## Hard constraints

1. **No `app/` code changes.** Phase 1c is entirely script + runbook.
2. **No new migrations / schema changes.** The UPDATE only flips
   status; it doesn't change column types or add columns.
3. **Strict per-event-type scoping.** The script MUST require
   `-EventType` (no default). Operator can't accidentally backfill
   everything at once.
4. **DryRun is default.** Script defaults to dry-run; live mode requires
   explicit `-DryRun:$false`.
5. **30-second inter-batch sleep is non-optional** (hardcoded). Prevents
   monopolizing the dispatcher and gives the handlers headroom.
6. **No backfill of `breakout_alert_resolved` or `backtest_completed`
   without the operator explicitly running per-event-type pre-flight
   memo D2.** Smaller event types (`paper_trade_closed`,
   `live_trade_closed`) can run with just the runbook.
7. **Phase 1b prereq is hard:** flag must be True AND 24h of stable
   handler activity in prod logs. CC must verify in the consult before
   any UPDATE.

## Open question for operator

What's the right `payload->>'backfill_source'` marker? Suggestions:

- `phase_1c_2026_05_<actual_date>` — date of the backfill run.
- `phase_1c_brain_event_kind_unify_backfill` — long but
  self-documenting.

Either lets post-hoc analysis tell synthetic backfill events from
organic ones. CC should surface the choice; no wrong answer.

## Success criteria

- D1 script committed and idempotent (re-running with same args is
  safe).
- D2 pre-flight memos for the two large event types.
- D3 runbook committed.
- CC_REPORT documents the operator's chosen order + marker.

## Next briefs in chain

- **Phase 2** (`f-adaptive-cpcv-gate.md`): adaptive CPCV gate redesign
  with empirical thresholds, Bayesian shrinkage, sample-size-aware
  CIs, Pareto frontier multi-objective. Now that Phase 1b/c restored
  the cpcv_gate handler's ability to actually run, Phase 2 can ship.
- **Phase 3** (`f-composite-quality-event-driven.md`): wire
  `quality_composite_score` as an event-driven node + backfill the
  584 NULL scores.
- **Phase 4** (`f-runtime-tab-surfacing.md`): UI for the new state
  classifications.
