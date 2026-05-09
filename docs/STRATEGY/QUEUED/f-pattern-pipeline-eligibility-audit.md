# f-pattern-pipeline-eligibility-audit

STATUS: QUEUED
SLUG: pattern-pipeline-eligibility-audit
PROPOSED: 2026-05-09
SEVERITY: medium (not breaking anything; identifying enablers for higher trade frequency without sacrificing quality)

## TL;DR

**Read-only research brief. No code changes shipped from this brief.**

Operator framing: "the recent crypto entries have been good so far,
they have been up... I don't want you to mess up the current working
system but just enhance it." Translation: don't loosen what works.
Identify whether the narrow funnel is a real bottleneck or
intentional, then propose additive enhancements (more discovery,
broader universe, multi-timeframe mining) — NOT gate-loosening.

The audit produces a single report:
`docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pattern-pipeline-eligibility-audit.md`.
The report's findings drive zero, one, or many follow-up briefs —
operator picks ordering. This brief itself ships no code.

## Why now

End-of-day 2026-05-08 state of `scan_patterns`:

| id | name | trades | WR | OOS | gates | status |
|---|---|---|---|---|---|---|
| 1011 | Reddit IBS mean reversion | 409 | 63.2% | NULL | (clean) | promoted |
| 1016 | Reddit IBS mean reversion | 565 | 70.7% | NULL | (clean) | promoted |
| 1047 | rsi_bullish_divergence | 4 | 25% | 50% | provisional | challenged |
| 585 | Intraday squeeze | 4 | 25% | NULL | provisional | challenged |

Two patterns of statistical substance, and they're essentially the
same edge in two evidence cohorts. That's not a portfolio — that's
one strategy.

Smoking gun from today's brain_work_events 24h totals:
- `backtest_completed: 268`
- **`pattern_eligible_promotion: 0`**

268 backtests ran. Zero patterns crossed the eligibility bar. The
miner is producing candidates, the backtest engine is running, the
promotion gate is rejecting everything. Without an audit we can't
tell if the gate is correctly tight or pathologically tight.

Operator-side context:
- Recent crypto entries (last 7d) net positive on the 12 open
  positions; 2 closed today at target with +$99.55.
- Pattern 585 was the noise-source (correctly demoted).
- The system is finding real edge — just sparingly.
- Volume of trades, not quality of trades, is the concern.

## Goal

Produce ONE report with the following sections:

### Section A: gate-rejection telemetry

For the 268 `backtest_completed` events in last 24h:
- For each, look up the corresponding `scan_patterns` row.
- Bucket by REASON the pattern didn't graduate to
  `lifecycle_stage='promoted'`:
  - CPCV gate fail (cross-validation didn't pass thresholds)
  - EV gate fail (realized expected value below threshold)
  - sample-size fail (too few backtest trades to statistically
    qualify)
  - OOS-NULL fail (missing out-of-sample evaluation)
  - regime-fail (didn't pass per-regime validation)
  - other (document)
- Output: bucket histogram with counts.

### Section B: human calibration

Sample 20 rejected patterns randomly. For each:
- Show the pattern name, the rejection reason, the realized stats
  (trade_count, win_rate, avg_return_pct, oos_win_rate).
- Manual classification by an algo-trader-architect read: would a
  reasonable human have promoted this? Y/N/Maybe with one-line
  justification.
- The goal is calibration: if 0/20 look promotable, the gate is
  correctly tight. If 5/20 look promotable but were rejected, the
  gate is mis-calibrated.

### Section C: distribution audits

- `evidence_count` distribution across all `scan_patterns`. If
  most patterns sit at evidence_count<3, the eligibility bar is
  structurally unreachable.
- `oos_win_rate IS NULL` count. If 95%+ of patterns have NULL OOS,
  the OOS evaluation pipeline is the choke point.
- `lifecycle_stage` histogram. How many `discovered` /
  `provisional` / `promoted` / `challenged` / `decayed` rows
  exist? Where in the funnel does volume fall off?
- `promotion_gate_reasons` distribution: of the patterns flagged
  with reasons, what's the most common rejection bucket?

### Section D: pipeline-cadence audits

- When did the miner last run? Look at `scan_patterns.created_at`
  timestamps for the most-recent rows. If the most-recent
  miner-discovered pattern is days old, mining has stalled.
- backtest cadence: 268 in 24h = ~11/hour. Is that the design
  cadence or below it? Check for any backtest queue depth.
- pattern_eligible_promotion historical: was it always 0/24h, or
  did it tick up in the past? If it was non-zero historically,
  what changed?

### Section E: universe + timeframe audit

- Distinct tickers represented in `scan_patterns` (last 30d
  miner activity).
- Distinct timeframes (`scan_patterns.timeframe` if such field
  exists, else inferred from pattern metadata).
- Crypto vs equity vs options breakdown of pattern discovery.
- Are there obvious universe gaps (e.g., zero patterns from
  mid-cap crypto, zero patterns from sub-1d timeframes)?

### Section F: prioritized recommendations

A ranked list of follow-up briefs the audit findings support,
each with:
- One-sentence problem statement
- Estimated CC scope (small / medium / large)
- Risk to existing-working-system (low / medium / high)
- Prerequisite check ("only if Section X finding shows Y")

The operator picks which (if any) to promote to NEXT_TASK.

## Acceptance criteria

1. Single report at
   `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pattern-pipeline-eligibility-audit.md`
   with all six sections (A-F) populated with concrete data.
2. **NO code changes shipped from this brief.** Any temptation
   to "while I'm here, fix the obvious one" should be resisted —
   surface the finding in Section F as a recommended brief
   instead.
3. Report includes raw query SQL used for each section so the
   operator can re-run.
4. Section F has at LEAST 3 prioritized follow-up briefs ranked
   by risk-adjusted impact.
5. Commit + push the report.
6. Mark NEXT_TASK as DONE.

## Brain integration (reuse, don't rewrite)

- `scan_patterns` table — direct read-only SQL.
- `brain_work_events` — direct read-only SQL.
- `app/services/trading/learning.py` — read source for the gate
  logic (CPCV thresholds, EV gate, etc.) to document the actual
  thresholds in the report.
- `app/services/trading/brain_work/dispatcher.py` — read source to
  document the eligibility-promotion flow.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **No code changes from this brief.** Read-only research only.
- **Do not loosen any gate threshold.** The current gates produce
  the trades that are working today.
- **Do not delete or modify any pattern row.** Audit reads only.
- **Tests use `_test`-suffixed DB.** (Won't apply for a read-only
  brief, but safety belt.)
- **No magic numbers** in any proposed follow-up briefs.

## Out of scope

- Any actual fix. The audit decides what fix (if any) ships next.
- Architectural rebuild Phase 1 (separate brief; complementary).
- Phase D wiring fix (already shipped).
- Any change to the entry-decision logic that's currently producing
  good crypto trades.

## Sequencing

1. Section A: gate-rejection telemetry (the smoking gun first).
2. Section B: human calibration (the load-bearing finding —
   tells us if the gate is correctly tight or pathologically
   tight).
3. Sections C-E: distribution + cadence + universe in parallel.
4. Section F: prioritized recommendations.
5. Commit + push the report.

## Operator-side after CC ships

1. Read the report.
2. Decide which (if any) of the Section F follow-up briefs to
   queue.
3. If gate-rejection telemetry reveals an obvious fix (e.g., a
   single threshold mis-calibrated by 2 orders of magnitude), the
   fix is a SEPARATE brief promoted to NEXT_TASK — not part of
   this audit.

## Rollback plan

N/A — read-only brief produces a report, nothing to roll back.
The report itself can be deleted if the operator wants it gone.

## What CC should do if it's unsure

1. **If a query is too expensive** (e.g., scanning the full
   `brain_work_events` table costs >30s), surface the cost and
   propose a sample-based approach.
2. **If the audit reveals an unambiguous bug** (not just
   mis-calibration — e.g., the gate function has a literal
   `return False` that shouldn't be there), surface in Section
   F's #1 recommendation as a hot-fix candidate, but DO NOT
   ship the fix from this brief.
3. **If the report is going to be long (>500 lines)**, split into
   two: this brief outputs a summary report (sections A-F at
   ~300 lines max) and the detailed query outputs go to a
   separate appendix file. Operator readability over completeness.
