# NEXT_TASK: f-pattern-pipeline-eligibility-audit

STATUS: PENDING

## Goal

Read-only research audit. **No code changes shipped from this brief.**

Operator framing: "the recent crypto entries have been good so far,
they have been up... I don't want you to mess up the current working
system but just enhance it."

The system is finding real edge today (DOT + SOL closed at target,
+$99.55 realized; 12 open crypto positions net up unrealized). The
issue isn't quality — it's that the eligibility funnel is producing
only one effective pattern (Reddit IBS mean reversion in two
evidence cohorts: id=1011 + id=1016). The audit identifies WHY the
funnel is narrow, then proposes additive enhancements as separate
follow-up briefs — never gate-loosening.

The full brief is at
`docs/STRATEGY/QUEUED/f-pattern-pipeline-eligibility-audit.md`
— read it first.

## Why now

End-of-day 2026-05-08 brain_work_events 24h totals:
- `backtest_completed: 268`
- **`pattern_eligible_promotion: 0`**

268 backtests ran today; zero patterns crossed the eligibility bar.
Either the gate is correctly tight (rejecting genuinely-poor
candidates) or pathologically tight (rejecting good candidates).
Without an audit we can't tell. The two scenarios call for
different responses:
- Correctly tight → expand discovery (broader universe,
  multi-timeframe mining, additive enhancements)
- Pathologically tight → fix the gate calibration

This audit is the diagnostic that tells us which.

## Why this scope

* **Vs. Phase 1 of the architectural rebuild**: that's a week of
  reconciler work; this is one read-only audit producing one
  report.
* **Vs. directly loosening the gate**: dangerous. Operator's
  current trades work because the gate is tight. Don't loosen
  blindly.
* **Vs. directly expanding the universe**: tempting but blind —
  if the gate is correctly tight, more universe = more rejected
  candidates with no observable benefit. Audit FIRST.
* **Vs. wiring f-pattern-oos-revalidation**: the natural next
  brief if Section F surfaces it; depends on the audit data.

## The change (audit produces report only)

Read-only SQL queries against `scan_patterns`, `brain_work_events`,
and source-code reads of `learning.py` + `brain_work/dispatcher.py`.

Six sections in the output report:

* **A**: gate-rejection telemetry — bucket the 268 backtests by
  rejection reason. The dominant rejector is the smoking gun.
* **B**: human calibration — sample 20 rejected patterns; algo-
  trader read on whether each looks promotable. Tells us if the
  gate is correctly or pathologically tight.
* **C**: distribution audits — `evidence_count`, OOS-NULL count,
  `lifecycle_stage` histogram, `promotion_gate_reasons` breakdown.
* **D**: pipeline-cadence audits — when did mining last run, was
  pattern_eligible_promotion always 0, etc.
* **E**: universe + timeframe audit — what tickers + timeframes
  are mined today; obvious gaps?
* **F**: prioritized recommendations — ranked follow-up briefs
  with scope + risk to existing-working-system + prerequisites.

## Acceptance criteria

1. Single report at
   `docs/STRATEGY/CC_REPORTS/2026-05-09_f-pattern-pipeline-eligibility-audit.md`
   with all six sections populated with concrete data.
2. **NO code changes** shipped from this brief. Any
   while-I'm-here fix temptations get surfaced in Section F as
   recommendations, not commits.
3. Report includes raw query SQL for each section so the operator
   can re-run.
4. Section F lists at least 3 prioritized follow-up briefs ranked
   by risk-adjusted impact, each with explicit risk-to-existing-
   system rating.
5. No mutation of any pattern row, work_ledger event, or DB
   state. Read-only.

## Brain integration (read-only)

- `scan_patterns` — direct SQL.
- `brain_work_events` — direct SQL (CC's tonight: this table is
  named `brain_work_events`, not `work_ledger`; verified earlier
  today).
- `app/services/trading/learning.py` — read for gate thresholds.
- `app/services/trading/brain_work/dispatcher.py` — read for
  eligibility-promotion flow.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **NO CODE CHANGES.** Read-only research.
- **DO NOT LOOSEN ANY GATE THRESHOLD.** Current gates produce the
  trades that are working today.
- **DO NOT DELETE OR MODIFY** any pattern row, work_ledger event,
  or DB state.
- **DO NOT WIDEN SCOPE** to actually fix anything surfaced — even
  if the fix looks obvious. Surface as Section F recommendation.
- **Edit-tool truncation discipline (HARD).**

## Out of scope

- Any actual fix.
- Loosening any gate or threshold.
- Universe expansion (if recommended, it's a separate brief).
- Multi-timeframe mining (if recommended, separate brief).
- OOS revalidation (if recommended, separate brief).
- The architectural rebuild Phase 1.
- Any change to entry-decision logic.

## Sequencing

1. Section A first — it's the smoking gun (where do the 268
   backtests die?).
2. Section B next — calibration check on whether the gate is
   correct or wrong.
3. Sections C-E in parallel — fact-gathering.
4. Section F last — the synthesis (with at least 3 ranked
   follow-up briefs).
5. Commit + push the report.

## Operator-side after CC ships

1. Read the report.
2. If Section A reveals a clear dominant rejector and Section B
   shows it's pathologically rejecting good patterns, queue a
   targeted fix brief.
3. If Section A reveals correctly-tight rejection but Section E
   shows obvious universe gaps, queue an additive-discovery
   brief (universe expansion, multi-timeframe).
4. If everything looks healthy and the funnel is just genuinely
   narrow because the universe of edges is narrow, accept the
   current state and pick a different priority.

## Rollback plan

N/A — read-only audit. Report can be deleted if the operator
wants it gone.

## What CC should do if it's unsure

1. **If a query is too expensive** (>30s scanning a large table),
   surface the cost and propose a sampled approach.
2. **If the audit reveals an unambiguous bug**, surface in
   Section F's #1 recommendation as a hot-fix candidate, but DO
   NOT ship the fix from this brief.
3. **If the report grows beyond 500 lines**, split into a summary
   (≤300 lines, all 6 sections) + a separate appendix file with
   detailed query output. Operator readability first.
4. **If the brain_work_events table or the pattern_eligible_
   promotion event-type doesn't exist** as expected, document the
   schema discrepancy in the report and propose how to verify
   the eligibility funnel anyway (e.g., via timestamps on
   `scan_patterns.lifecycle_changed_at` or similar).
