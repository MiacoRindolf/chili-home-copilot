# NEXT_TASK: f-coinbase-post-place-verify-routing-fix

STATUS: DONE

## Goal

Tick-size fix (commit 5f6576a) sealed price quantization — Coinbase REST
now ACCEPTS stop orders and returns order IDs. But the post-place
verification step calls Robinhood's API for Coinbase orders, gets 404,
marks intent 'unverified', and never persists `broker_stop_order_id`.

The 4 Coinbase orders that got valid IDs (AERGO, 1INCH, ACX, RARE) may
actually be sitting at Coinbase as live stops — DB just doesn't see
them. Each sweep may be attempting re-placement (cooldown is largely
preventing duplicates).

## Brief

`docs/STRATEGY/QUEUED/f-coinbase-post-place-verify-routing-fix.md`.

## Phases

Single-shot fix.

## Deliverables

- Verify-routing fix in `bracket_writer_g2.py` (mirror the place-side
  `_SUPPORTED_VENUES` pattern)
- `get_order_status` primitive on Coinbase adapter if missing
- Tests in `tests/test_coinbase_post_place_verify.py`
- Orphan-recovery: how the next sweep auto-confirms the 4+ already-placed
  Coinbase orders without re-placing
- CC_REPORT
- NEXT_TASK → STATUS: DONE

## Hard constraints

- `bracket_writer_g2.py` + `coinbase_spot.py` only.
- Edit-tool truncation discipline.
- Phase 6 LIVE soak active — additive routing branch.
- No magic-fallback values.
- Plan-gate active.

## Resolution (2026-05-10)

Done in three checkpoint commits on `main`:

- `21ce9ee` — wip(brain): coinbase `get_order_status` normalizes state
  vocabulary
- `7def71b` — feat(brain): venue-route post-place verify + Coinbase
  orphan recovery
- `c8a3ff3` — test(brain): coinbase post-place verify coverage +
  orphan recovery

22 new tests pass (`pytest tests/test_coinbase_post_place_verify.py
-v -p no:asyncio` → 22 passed in 1.23s). Truncation scan clean on
both production files (`bracket_writer_g2.py` 1603 → 1797, AST OK;
`coinbase_spot.py` 1358 → 1450, AST OK).

Full CC_REPORT:
`docs/STRATEGY/CC_REPORTS/2026-05-10_f-coinbase-post-place-verify-routing-fix.md`.

**DEPLOY BLOCKER carried forward** from the plan-gate response:
`stop_engine.py` (1302 vs HEAD 1316) and
`bracket_reconciliation_service.py` (2276 vs HEAD 2577) are still
truncated on disk from the 2026-05-10 19:42Z bracket-coverage-fix-v2
session. Operator must restore both before
`docker compose up -d --force-recreate <workers>` or the fix can't
deploy (importing either file crashes the workers).
