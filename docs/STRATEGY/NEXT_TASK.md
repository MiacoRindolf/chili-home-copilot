# NEXT_TASK: f-coinbase-orphan-stop-adoption

STATUS: DONE

CC_REPORT: `docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-orphan-stop-adoption.md`

## Goal

Verify-routing fix (commit `c8a3ff3`) sealed the Robinhood-404 problem
but exposed that 4 Coinbase trades (AERGO, 1INCH, ACX, RARE) have
orphan stops live at the venue holding qty in reserve. New placement
attempts now fail with "Insufficient balance in source account".

Build a one-shot adoption pass that lists Coinbase open stops, matches
them to bracket_intent rows by ticker, and persists `broker_stop_order_id`
so the system adopts the orphans without canceling them.

## Brief

`docs/STRATEGY/QUEUED/f-coinbase-orphan-stop-adoption.md`.

## Phases

Single-shot.

## Deliverables

- Adoption-pass module (Coinbase venue adapter neighborhood)
- `dispatch-coinbase-orphan-adopt.ps1` if option A picked
- Tests in `tests/test_coinbase_orphan_adopt.py`
- CC_REPORT
- NEXT_TASK → STATUS: DONE

## Hard constraints

- Coinbase venue adapter + new adoption module + new test only.
- Edit-tool truncation discipline.
- Phase 6 LIVE soak active — purely additive.
- No magic-fallback qty/ticker matching. Ambiguous = skip + log.
- Plan-gate active.
