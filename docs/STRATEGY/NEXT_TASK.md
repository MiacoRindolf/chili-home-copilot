# NEXT_TASK: f-promotion-pipeline-rebalance

STATUS: PHASE_4_DONE (Phases 5-6 ship in subsequent CC sessions per brief sequencing)

## Goal

**Algo-trader-architect overhaul of the brain's pattern promotion
pipeline.** The brain mines plenty of high-quality patterns (769 active,
586 mineable) but only **3 are promoted** — pattern 1011, 1016, and 585
(re-promoted manually 2026-05-09 after a noise-driven auto-demote).

The full brief is at
`docs/STRATEGY/QUEUED/f-promotion-pipeline-rebalance.md`.

## Phase status

- Phase 1 — sample-size floor + AND-logic auto-demote — **DONE** (16/16 tests)
- Phase 2 — directional-correctness signal — **DONE** (mig 235; pattern 585 directional WR=73.3%)
- Phase 3 — shadow_promoted lifecycle stage — **DONE** (mig 236; RH parity hard gate PASSED)
- Phase 4 — composite quality scoring + cohort auto-promote — **DONE** (mig 237; ships dormant)
- Phase 5 — per-pattern universe via `scope_tickers` — **NEXT**
- Phase 6 — 7-day verification + final summary

## Phase 4 incident note (2026-05-10)

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
— only modify files explicitly listed in the plan's section (a). The
Phase 5 .session prompt at
`scripts/_claude_session_queue/400-promotion-rebalance-phase5.session`
encodes these rules.

## Currently-armed state

- Coinbase Phase 6 paper-soak LIVE through 2026-05-11 (real $150 max)
- Phases 1-4 of f-promotion-pipeline-rebalance shipped (Phase 4 dormant,
  flag default False until operator opts in)
- Pattern 585 in promoted, alerts firing
- Eligible promoted patterns: 3 (pre-Phase-4 cohort promote)
