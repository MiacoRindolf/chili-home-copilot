# f-evidence-fidelity-architecture (2026-05-14)

> **Type:** Architect proposal — 5-phase initiative
> **Priority:** P0 — foundational evidence-quality fix that improves
> every downstream decision (gate, sizing, autotrader). Identified during
> algo-trader-architect investigation 2026-05-14 (this conversation).
> **Status:** all 5 phase briefs pre-written below (learning from
> adaptive-promotion arc where unwritten Phase 3/4 briefs stalled the
> chain).

## TL;DR

Today's investigation found **the brain has 4 critical wiring gaps**
between code that exists and code that runs:

1. **Counterfactual-corrected** and **raw-realized** pattern stats race
   to overwrite the same columns. Pattern 585 currently shows the
   *dumber* numbers (83/34.9%) because `realized_stats_sync` writes last.
2. **`record_fill_observation`** (venue truth) has zero production
   callers despite 15k execution events available. Tables empty by design.
3. **`label_snapshots`** (triple-barrier labels) has no scheduler.
   Table at 0 rows. The meta-classifier alpha layer is dead.
4. **NetEdge ranker** receives `scan_pattern_id=null, regime=unknown`
   on every row because the live autotrader bypasses the allocator
   that calls it.
5. **`n_hypotheses_tested=1`** is hardcoded in cpcv_gate. The DSR
   multiple-testing correction is effectively disabled.

None of these require new alpha models. They're all **"connect the
pipes that already exist."** Aggregate impact: bigger than the adaptive
promotion arc that just shipped, because they fix evidence *quality*
that every gate consumes.

## Why this is different from the prior arc

The adaptive-promotion arc (2026-05-11) fixed how patterns get *gated*
once their evidence is in. This arc fixes the **evidence itself** —
the inputs every gate reads. Stronger inputs → every gate works better
without any new gating logic.

Phases are largely parallel after Phase A. Operator-controlled
dependency: Phase A (canonical outcome split) is foundational; everyone
reads its columns. B/C/D/E can run concurrently after A.

## Phase plan (briefs pre-written below)

| Phase | Title | Effort | Depends on |
|---|---|---|---|
| A | Canonical Outcome Layer (split corrected vs raw columns) | ~150 LOC + mig | none |
| B | Execution Truth Wiring (record_fill_observation in fill path) | ~50 LOC | none |
| C | Triple-Barrier Activation (scheduler + first labels) | ~30 LOC | none |
| D | NetEdge Live Wiring (autotrader → allocator round-trip) | ~100 LOC | A (corrected stats feed) |
| E | Multiple-Testing Discipline (n_hypotheses_tested family count) | ~20 LOC + helper | A (for family grouping) |

## Operator open questions to answer in plan-gate consults

- **Phase A:** When the corrected and raw values disagree by >20%, do
  we emit an audit alert or just shadow-log? (default: shadow-log; raise
  alert only if >50% delta).
- **Phase B:** What's the source-of-truth for expected_cost_fraction?
  (brief assumes bracket_intent.expected_*; if it's `cost_aware_gate`
  pre-trade output, swap).
- **Phase C:** Scheduler cadence — 4h vs 6h? (default 4h).
- **Phase D:** Wholesale move autotrader → allocator, or add a
  parallel call from autotrader to NetEdge for shadow-feed? (default:
  parallel call first, full integration after Phase D soak.)
- **Phase E:** Family grouping rule — `hypothesis_family` column vs
  derive from `name LIKE 'X [variant-N]'`? (brief default: use
  `hypothesis_family` column; fall back to parent_id chain.)

## Files / briefs created by this arc

- `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md` (this)
- `docs/STRATEGY/QUEUED/f-canonical-outcome-layer.md` (Phase A)
- `docs/STRATEGY/QUEUED/f-execution-truth-wiring.md` (Phase B)
- `docs/STRATEGY/QUEUED/f-triple-barrier-activation.md` (Phase C)
- `docs/STRATEGY/QUEUED/f-netedge-live-wiring.md` (Phase D)
- `docs/STRATEGY/QUEUED/f-multiple-testing-discipline.md` (Phase E)
