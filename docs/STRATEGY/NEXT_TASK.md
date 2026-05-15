# NEXT_TASK: f-canonical-outcome-layer

STATUS: PENDING

## Goal

**Phase A of evidence-fidelity-architecture (2026-05-14).** Foundational.
Stop the silent race between corrected and raw-realized pattern stats
that overwrite the same columns. Split into `corrected_*` and
`raw_realized_*`, make every downstream gate read corrected only.

Pattern 585 currently shows the *dumber* numbers (83/34.9% raw-realized)
instead of the corrected ones (87/39.8%) because `realized_stats_sync`
writes last. This bug undermines every promotion / sizing / autotrader
decision.

## Brief

`docs/STRATEGY/QUEUED/f-canonical-outcome-layer.md`

Parent architectural brief:
`docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

Phase briefs (pre-written; watcher promotes after each phase lands):

- **Phase A** — canonical outcome split (THIS task)
- **Phase B** — `f-execution-truth-wiring.md` (parallel after A)
- **Phase C** — `f-triple-barrier-activation.md` (parallel after A)
- **Phase D** — `f-netedge-live-wiring.md` (after A)
- **Phase E** — `f-multiple-testing-discipline.md` (after A)

## Deliverables

1. `app/migrations.py` — migration N+1 adds 8 columns (corrected_* +
   raw_realized_*)
2. `app/services/trading/learning.py` — `update_pattern_stats_from_closed_trades`
   dual-writes corrected_* and legacy columns
3. `app/services/trading/realized_stats_sync.py` — writes ONLY
   raw_realized_*; never touches legacy
4. Reader updates in promotion_gate, realized_ev_gate, cpcv_adaptive_gate,
   auto_trader, pattern_quality_score
5. `scripts/canonical-outcome-backfill.ps1` — one-shot historical
6. `tests/test_canonical_outcome_layer.py` — race test
7. CC_REPORT

## Hard constraints

- Legacy `{trade_count, win_rate, avg_return_pct}` stay populated
  (mapped to corrected_*) — no consumer breaks at merge
- Migration is additive only
- Backfill idempotent + `-DryRun` default + kill switch
- No autotrader / venue / broker behavior change at merge
- TEST_DATABASE_URL must end in `_test`

## Consult gate

When corrected vs raw disagree by >20%, audit alert or shadow-log?
Brief default: shadow-log only; alert at >50% delta.
