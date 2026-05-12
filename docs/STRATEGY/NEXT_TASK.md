# NEXT_TASK: f-runtime-tab-surfacing

STATUS: DONE

Shipped 2026-05-11. Closes the adaptive-promotion-architecture arc.
All phases (0/1a/1b/1c/2/3/4) shipped today.

CC_REPORT: `docs/STRATEGY/CC_REPORTS/2026-05-11_runtime-tab-surfacing.md`

## Architecture arc complete

| Phase | Status |
|---|---|
| 0  | ✅ CPCV gate coverage audit |
| 1a | ✅ Dispatcher silence audit |
| 1b | ✅ Event-kind unify + prod flag flipped |
| 1c | ✅ Backfill script + memos |
| 2  | ✅ Adaptive CPCV gate |
| 3  | ✅ Composite quality event-driven |
| 4  | ✅ Runtime tab surfacing |

## Operator-controlled follow-ups (optional)

- Run `scripts/brain-event-backfill.ps1` — drought relief (1055 historical backtest_completed events)
- Run `scripts/quality-score-backfill.ps1` — populate 584 NULL composite scores
- Flip `chili_cpcv_adaptive_gate_enabled=1` — activate Phase 2 adaptive gate
- Dev-system reliability fixes (separate brief)
