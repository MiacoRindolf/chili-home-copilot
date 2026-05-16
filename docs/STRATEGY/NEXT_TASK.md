# NEXT_TASK: f-netedge-live-wiring

STATUS: DONE

## Goal

**Phase D of evidence-fidelity-architecture.** Wire a shadow
`net_edge.score(...)` call from the live autotrader path with the full
context (scan_pattern_id, regime, timeframe, asset_class). Today every
recent NetEdge row has `scan_pattern_id=null, regime=unknown` because
the live autotrader bypasses `portfolio_allocator.evaluate()` which is
where NetEdge is currently fed.

Stage 1 only — shadow log. Stage 2 (NetEdge as authoritative gate
input) is a future brief.

## Brief

`docs/STRATEGY/QUEUED/f-netedge-live-wiring.md`

Parent: `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

Prior phases shipped:
- A `ca1705f` — canonical outcome split
- B `51da8cc` — execution-truth wiring
- C `340215f` — triple-barrier label scheduler

## Deliverables (per brief)

1. `app/services/trading/auto_trader.py` — shadow `score(...)` call in `_process_one_alert`
2. `app/services/trading/crypto_autotrader.py` (if separate) — same hook
3. Regime-snapshot diagnostic check
4. `tests/test_netedge_autotrader_wiring.py`
5. CC_REPORT

## Hard constraints

- NO change to live trade decision path. NetEdge score is shadow-log only at merge
- `brain_net_edge_ranker_mode` stays "shadow"
- Reads `corrected_*` columns (Phase A dependency)
- Failure of `net_edge.score(...)` MUST NOT block autotrader (try/except wrap)
- No autotrader / venue / broker behavior change
- TEST_DATABASE_URL must end in `_test`

## After D

Phase E (multiple-testing discipline) brief written and `.session` queued
at priority 340 — daemon will pick it up after D completes.
