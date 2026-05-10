# NEXT_TASK: f-coinbase-post-place-verify-routing-fix

STATUS: PENDING

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
