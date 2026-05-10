# Phase 6 Plan Request — f-promotion-pipeline-rebalance final summary + CURRENT_PLAN update

**Session**: `promotion-rebalance-phase6-2026-05-10` (interactive Cowork)
**Risk**: LOW (doc-only, no code touched)
**Mode**: PLAN-GATE active; doc-only changes; no migrations, tests, or .py edits

---

## ⚠️ Reality-check flag for Cowork (single sentence)

The session prompt says "NEXT_TASK.md should be PHASE_5_DONE", but on disk
NEXT_TASK is `STATUS: PHASE_4_DONE`, no Phase 5 commit exists in `git log`,
the `400-promotion-rebalance-phase5.session` daemon run errored at
launcher resolution (`Execution error` after invoking claude.exe; stderr
empty), and no Phase 5 CC_REPORT or COWORK_REVIEW exists. **Phase 5
(per-pattern universe via `scope_tickers`) did NOT ship.** Phase 6 below
is structured as the wrap-up of Phases 1–4 with Phase 5 explicitly carried
forward as deferred / unstarted work. Operator/Cowork: confirm this framing
or reply REVISE with a different scope for Phase 6.

---

## (a) Files to write/modify — all docs, no code

1. **NEW** `docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase6-final-summary.md`
   — Final summary report covering Phases 1-4. Format-compliant per
   PROTOCOL.md `CC_REPORT format`. Includes verification query results
   captured live from the running `chili` Postgres (see section (d)).

2. **MODIFY** `docs/STRATEGY/CURRENT_PLAN.md` — ADD a new "Parallel
   initiative" section for f-promotion-pipeline-rebalance. The
   CURRENT_PLAN top-level initiative is `Position Identity Refactor`
   (Phase 1 in soak through 2026-05-11); the promotion-rebalance work is
   architecturally a separate parallel track and should be documented as
   such, not "replace the old promotion-pipeline section" (no such
   section exists — the brief lives in
   `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md`). Mirrors how
   `f-coinbase-autotrader-enablement` is documented at lines 65-118 today.

3. **MODIFY** `docs/STRATEGY/NEXT_TASK.md` — flip `STATUS:` from
   `PHASE_4_DONE (Phases 5-6 ship in subsequent CC sessions...)` to
   `STATUS: DONE` with a postscript noting Phase 5 was deferred and the
   initiative is closed pending operator queueing the next one.

4. **NO `.py` files modified.** No new tests. No new migrations.
   Truncation risk = 0 by construction.

**Out-of-scope (will NOT touch)**:
- Any file in `app/`, `scripts/` (other than session-consult dir), or `tests/`.
- Any pattern row in `scan_patterns` (no `UPDATE` SQL).
- Any feature flag in `.env`.
- The Position-Identity-Refactor sections of CURRENT_PLAN.md (only
  ADDING a new parallel-initiative section; existing content unchanged).

---

## (b) Final summary report structure

The new CC_REPORT will follow this outline:

```markdown
# CC_REPORT: f-promotion-pipeline-rebalance — Phase 6 (Final summary)

## Initiative outcome
One-paragraph verdict. The pipeline rebalance shipped 4 of 6 phases
(Phases 1-4 in repo; Phase 5 deferred; Phase 6 is this doc). The four
shipped phases together deliver the architectural rebalance the brief
promised: clean directional eval signal + risk-asymmetric lifecycle stage
+ composite quality scoring + automated cohort ramp — all of it dormant
behind the kill-switch flag (`chili_cohort_promote_enabled=False`)
pending operator opt-in.

## Initiative goals (recap from brief)
The brief at QUEUED/f-promotion-pipeline-rebalance.md identified four
architectural problems:
  1. Promotion gate and trade gate were conflated.
  2. Realized P&L was contaminated by autotrader-gate noise.
  3. Auto-demote used single-condition OR logic on small-n samples.
  4. No cohort-promotion ramp; no per-pattern universe.
Phases 1-4 address problems 1-3 directly and lay the foundation for #4
(cohort ramp). Problem #4's per-pattern-universe half is deferred (Phase 5
unstarted).

## What shipped per phase
- Phase 1 (b00edec): sample-size floor + AND-logic CPCV protection
  - 2 settings (min_realized_trades=30, require_cpcv_degrade=True)
  - 2 demote paths fixed (Phase D sweep + 02:15 PT audit)
  - 16/16 tests PASS
  - **Pattern 585 protected** (n=8, CPCV=1.40)
- Phase 2 (e480d9f): directional-correctness signal
  - Migration 235: pattern_alert_directional_outcome + view
  - 5 settings + new evaluator module + 30-min scheduler job
  - 19/19 tests PASS
  - **Pattern 585 directional WR=73.3%** (vs gate-laundered 25%)
- Phase 3 (ba05195): shadow_promoted lifecycle stage
  - Migration 236: CHECK constraint widened
  - 1 flag + helper + autotrader splice
  - **Parity hard-gate PASSED**: byte-identical for non-shadow patterns
- Phase 4 (893e73c): composite quality scoring + cohort auto-promote
  - Migration 237: scan_patterns.quality_composite_score column
  - 8 settings (5 weights summing to 1.00 + cohort cap params)
  - 2 new modules + 2 scheduler jobs (nightly score, weekly cohort)
  - **Pattern 585 composite=0.843** (matches plan-gate calibration)
  - Ships **DORMANT** (chili_cohort_promote_enabled=False)

## Architectural delta (before/after)
Before: lifecycle ∈ {candidate, backtested, validated, challenged,
promoted, live, decayed, retired}; one promotion ladder; demote on any
single thin-evidence trigger; no clean directional signal.
After: lifecycle adds `shadow_promoted` (observation, no execution); two
ladders (alert eligibility vs trade eligibility); demote requires CPCV
degrade AND realized degrade; gate-noise-free directional WR available
per pattern via rolling-30 view.

## Verification (live SQL captured this session — see section (d) below)
Three queries executed against the running `chili` DB inside
`chili-home-copilot-postgres-1` to capture actual numbers post-Phase-4:
  1. lifecycle_stage distribution
  2. pattern_directional_quality_v top 10 by sample size
  3. quality_composite_score top 20 (NULL-excluded)

## Surprises / deviations
- Phase 4 incident: Edit-tool truncated 8 unrelated large files; brain-side
  work salvaged by Cowork. Lesson encoded in advisor brief §2.1.
- Pattern 585's directional WR (73.3%) vs gate-laundered realized WR (25%)
  retroactively justified the entire initiative.
- Phase 5 (per-pattern universe) did not ship: launcher errored at
  daemon startup; no commit; no CC report. Carried forward as deferred.

## Risks carried
- Phase 4 ships dormant. Until operator flips
  CHILI_COHORT_PROMOTE_ENABLED=true, the cohort ramp does not run; the
  brain still depends on operator-manual moves to populate
  `shadow_promoted`.
- Phase 4 tests written but not all executed at session-end (DB
  contention with parallel pytest); operator should run
  `pytest tests/test_pattern_cohort_promote.py -v -p no:asyncio` once
  before opt-in.
- Phase 5 deferred: patterns with `scope_tickers` set (e.g., 1011, 1016)
  still fall back to global universe; off-hours skip-rate elevated.

## What's next
- Operator runs the cohort-promote tests and force-recreates with
  `CHILI_COHORT_PROMOTE_ENABLED=true` when ready.
- Phase 5 (per-pattern universe via scope_tickers) is queued for a
  future initiative; the brief at
  `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md` Phase 5 spec
  remains current and self-contained.
- Initiative closes; operator queues next initiative via NEXT_TASK.md.

## Hard rules check
- ✅ Hard Rule 1 (live-placement safety belts): every shipped phase is
  additive; new restrictive paths only.
- ✅ Hard Rule 5 (prediction-mirror authority): no `[chili_prediction_ops]`
  contract changes.
- ✅ Migration IDs sequential (235, 236, 237), idempotent.
- ✅ All new behavior gated by feature flags.
```

---

## (c) CURRENT_PLAN.md update spec

Add a new top-level section after the existing "Parallel initiative —
Coinbase autotrader enablement" block (currently at lines 65-118),
titled exactly:

```markdown
## Parallel initiative — Promotion-pipeline rebalance (Phases 1-4 SHIPPED)

f-promotion-pipeline-rebalance is a parallel multi-phase initiative
addressing the brain's promotion pipeline (orchestrated outside the
position-identity refactor and the Coinbase autotrader rollout —
disjoint surfaces).

**Final architecture (post-Phase-4)**:

* **Two-ladder lifecycle** with new `shadow_promoted` stage (Phase 3,
  mig 236). Patterns at `shadow_promoted` fire imminent alerts but
  autotrader routes their alerts to shadow-log only. `promoted/live`
  remains the trade-eligibility ladder; `shadow_promoted` is the
  alert-eligibility-only ladder.
* **Gate-noise-free directional signal** (Phase 2, mig 235). New table
  `pattern_alert_directional_outcome` + view `pattern_directional_quality_v`
  measure "did price move ≥1.5% in predicted direction within 24h" on
  every imminent alert (not just gate-survivors). Rolling-30 per-pattern
  WR is the clean signal.
* **AND-logic auto-demote with sample-size floor** (Phase 1). Patterns
  with `trade_count < 30` are protected from realized-stat demotes;
  patterns with `cpcv_median_sharpe ≥ 1.0` are protected even at higher
  n (CPCV must agree before demote). Settings:
  `chili_pattern_demote_min_realized_trades` (=30),
  `chili_pattern_demote_require_cpcv_degrade` (=True).
* **Composite quality score** (Phase 4, mig 237). Per-pattern
  `quality_composite_score ∈ [0,1]` =
  0.30·clip(cpcv_sharpe/2.0) + 0.20·clip(deflated_sharpe/1.0)
  + 0.15·(1−pbo) + 0.25·directional_wr + 0.10·(1−decay)
  computed nightly at 23:30 PT.
* **Weekly cohort auto-promote** (Phase 4). Sunday 22:00 PT job
  selects top-N candidates (capped at 10/rolling-7-day) by composite
  score and advances them to `shadow_promoted`. Eligibility requires
  `promotion_gate_passed=True`, `cpcv_median_sharpe≥1.0`,
  `rolling_sample_n≥30` (decay computable), and excludes
  already-promoted/shadow_promoted/live patterns. **Ships DORMANT**:
  `chili_cohort_promote_enabled=False` until operator opts in.

**Status (2026-05-10)**:

* Phase 1 SHIPPED (commit b00edec): sample-size floor + CPCV protection.
* Phase 2 SHIPPED (commit e480d9f): directional outcome + view +
  evaluator + 30-min scheduler.
* Phase 3 SHIPPED (commit ba05195): shadow_promoted lifecycle +
  byte-identical parity gate held.
* Phase 4 SHIPPED (commit 893e73c): composite scoring + cohort job
  (dormant by default).
* Phase 5 DEFERRED: per-pattern universe via `scope_tickers`. Session
  errored at daemon launch; no commit. Brief preserved at
  `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md` for future
  re-queue.
* Phase 6 = this doc.

**Calibration evidence** (the data point the brief was betting on):

Pattern 585 was the marquee case. Pre-Phase-1 it was auto-demoted on
n=8 gate-laundered trades (realized WR 25%); after the rebalance:

| Metric                  | Value | Source                            |
|-------------------------|-------|-----------------------------------|
| CPCV median Sharpe      | 1.40  | pre-existing                      |
| Deflated Sharpe         | 1.0   | pre-existing                      |
| PBO                     | 0.0   | pre-existing                      |
| Directional WR (rolling-30) | 0.733 | Phase 2 view                  |
| Composite score         | 0.843 | Phase 4 formula (top tier)        |

The realized WR (gate-laundered noise) and the directional WR (clean
signal) diverge by 48 percentage points on this pattern. That is the
quantified justification for the entire initiative.

**Operator opt-in (when ready)**:

1. Run pytest on the cohort suite:
   `pytest tests/test_pattern_cohort_promote.py -v -p no:asyncio`
2. Set `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env`.
3. `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker`
4. Wait for next Sunday 22:00 PT cohort job; inspect:
   `SELECT id, name, lifecycle_stage, quality_composite_score FROM scan_patterns WHERE quality_composite_score IS NOT NULL ORDER BY quality_composite_score DESC LIMIT 20;`

**Kill switch**: `CHILI_COHORT_PROMOTE_ENABLED=false` halts the weekly
job at the flag check; nightly score refresh continues
(non-destructive). Code revert: `git revert` Phase 4 commit; mig 237
(`ADD COLUMN IF NOT EXISTS`) intentionally left in place — harmless.
```

---

## (d) Verification queries to capture in the summary

Run these inside the `chili-home-copilot-postgres-1` container via
`docker exec` and paste the actual output into the summary report.
Read-only — no INSERT/UPDATE/DELETE.

```sql
-- Q1: Lifecycle stage distribution (proves shadow_promoted exists post-mig 236)
SELECT lifecycle_stage, COUNT(*) AS n
  FROM scan_patterns
 WHERE active = TRUE
 GROUP BY lifecycle_stage
 ORDER BY n DESC;

-- Q2: Top-10 patterns by directional sample size (proves Phase 2 evaluator
-- is populating; will likely show 585, 586 at sample_n=30 with WR=0.733)
SELECT scan_pattern_id, rolling_sample_n, wr, last_alert_at, last_evaluated_at
  FROM pattern_directional_quality_v
 ORDER BY rolling_sample_n DESC NULLS LAST, wr DESC NULLS LAST
 LIMIT 10;

-- Q3: Top-20 patterns by composite quality score (proves Phase 4 column
-- exists; if NULL across the board, the nightly job hasn't run yet
-- post-mig-237 deploy and operator should be advised to wait/manual-trigger)
SELECT id, name, lifecycle_stage, quality_composite_score
  FROM scan_patterns
 WHERE quality_composite_score IS NOT NULL
 ORDER BY quality_composite_score DESC, id ASC
 LIMIT 20;
```

If Q3 returns 0 rows, the summary will note: "Phase 4 score-refresh job
has not yet run against the live DB (mig 237 column exists; column is
NULL for all rows). Operator action: force-recreate the worker stack to
deploy Phase 4 code, then either wait for nightly 23:30 PT cron OR
manually invoke `compute_and_persist_scores` from the
scheduler-worker container."

---

## Section (g) — anything unclear from the prompt

Two ambiguities in the session prompt resolved by my reading of repo state:

1. **"NEXT_TASK.md should be PHASE_5_DONE"** — actually PHASE_4_DONE on
   disk; flagged at top of this request. I'm proceeding under the
   assumption that Phase 6 closes the initiative with Phase 5 deferred,
   not that I should silently retro-implement Phase 5. If Cowork wants
   Phase 5 implemented before the wrap-up, REVISE this plan and I'll
   ABORT (Phase 5 needs code, which is outside Phase 6's hard
   constraint).

2. **"replace the old promotion-pipeline section with the new
   architecture"** — there is no existing promotion-pipeline section in
   `CURRENT_PLAN.md`. The current plan is `Position Identity Refactor`.
   I'm proposing to ADD a new "Parallel initiative" section mirroring
   the existing Coinbase one, not replace anything. If Cowork wanted
   a different layout (e.g., overwrite the Position-Identity initiative
   with the rebalance), REVISE.

---

## Approval criteria

This is doc-only LOW-risk. No `.py` files touched. No tests. No
migrations. No `.env` changes. No flag flips. No SQL writes — only
three read-only `SELECT`s captured into the report. The watcher should
auto-approve unless Cowork wants to revise framing or scope on the two
ambiguities above.

Awaiting `plan.response.md` with `APPROVED` / `REVISE: <feedback>` /
`ABORT: <reason>`.
