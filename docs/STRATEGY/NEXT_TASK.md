# NEXT_TASK: f-promotion-pipeline-rebalance

STATUS: DONE

## Goal

**Algo-trader-architect overhaul of the brain's pattern promotion
pipeline.** The brain mines plenty of high-quality patterns (769 active,
586 mineable) but only 3 are promoted — pattern 1011, 1016, and 585
(re-promoted manually 2026-05-09 after a noise-driven auto-demote).

The full brief is at
`docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md`.

## Phase status (final)

- Phase 1 — sample-size floor + AND-logic auto-demote — **SHIPPED**
  (commit `b00edec`; 16/16 tests)
- Phase 2 — directional-correctness signal — **SHIPPED**
  (commit `e480d9f`; mig 235; pattern 585 directional WR=73.3% at ship,
  96.7% at Phase 6 verification)
- Phase 3 — shadow_promoted lifecycle stage — **SHIPPED**
  (commit `ba05195`; mig 236; RH parity hard gate PASSED)
- Phase 4 — composite quality scoring + cohort auto-promote —
  **SHIPPED** (commit `893e73c`; mig 237; ships dormant)
- Phase 5 — per-pattern universe via `scope_tickers` — **DEFERRED**
  (session errored at daemon launch; no commit. Brief preserved at
  `docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md` Phase 5
  section for future re-queue.)
- Phase 6 — final summary + CURRENT_PLAN update — **DONE**
  (`docs/STRATEGY/CC_REPORTS/2026-05-10_f-promotion-pipeline-rebalance-phase6-final-summary.md`)

## Closure

Initiative closes 2026-05-10. CURRENT_PLAN.md updated with the new
architecture in a "Parallel initiative — Promotion-pipeline rebalance"
section below the Coinbase autotrader block. Operator queues the next
initiative when ready.

**Pending operator action before opt-in to Phase 4 cohort ramp**:

1. `pytest tests/test_pattern_cohort_promote.py -v -p no:asyncio`
2. `docker compose up -d --force-recreate chili scheduler-worker
   brain-worker autotrader-worker broker-sync-worker` (deploys Phase 4
   code; nightly score-refresh starts at next 23:30 PT).
3. When ready for the weekly cohort job: set
   `CHILI_COHORT_PROMOTE_ENABLED=true` in `.env` and force-recreate.

## Phase 4 incident note (preserved for the record, 2026-05-10)

CC's Phase 4 session was killed mid-flight after the Edit tool truncated
8 large unrelated files (auto_trader.py, broker_service.py, coinbase_spot.py,
bracket_writer_g2.py, brain_work/dispatcher.py, learning.py, pdt_guard.py,
promotion_evidence_audit.py — combined 1743 lines deleted). The brain-side
intended Phase 4 work was clean and matched the plan-gate-approved
bindings exactly. Cowork salvaged the brain-side work directly and
committed it; the truncated files were restored from HEAD via the nuclear
delete-then-restore pattern. See
`docs/STRATEGY/COWORK_REVIEWS/2026-05-10_f-promotion-pipeline-rebalance-phase4.md`
for full incident detail.

**Lesson encoded for Phase 5+**: For files >500 lines, use `Write`
(full overwrite) NOT `Edit`. After every edit: `wc -l` + `git diff
--stat` + AST parse. If `wc -l` drops more than your edit added: STOP,
restore from HEAD, switch to `Write`. Scope discipline is also critical
— only modify files explicitly listed in the plan's section (a).
