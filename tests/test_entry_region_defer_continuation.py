"""#7 entry-region latency — ang stale-at-seam defer ay nagpapatuloy IN-TICK.

BCCQ/ADXN 2026-08-21: 58-117s na butas sa candidate->submit dahil ang
pre_place_blocked defer ay bumabalik sa WATCHING nang walang continuation —
ang re-climb ay naghihintay ng scheduler cadence bawat baitang. Source
contracts (ang cadence mismo ay hindi masusukat sa unit test — ang Lunes
premarket probe ang live na sukat).
Runnable: pytest tests/test_entry_region_defer_continuation.py -v
"""
from __future__ import annotations

import inspect

from app.services.trading.momentum_neural import live_runner


def _tick_src() -> str:
    return inspect.getsource(live_runner.tick_live_session)


def test_pre_place_blocked_schedules_continuation():
    src = _tick_src()
    blocked_at = src.index('if res.get("pre_place_blocked"):')
    governor_at = src.index('if res.get("deferred"):', blocked_at)
    seg = src[blocked_at:governor_at]
    assert "_schedule_entry_fsm_continuation(sess.id)" in seg, \
        "stale-at-seam defer must re-climb in-tick"
    # Ang continuation ay PAGKATAPOS ng transition sa WATCHING at bago ang return.
    assert seg.index("STATE_WATCHING_LIVE") < seg.index(
        "_schedule_entry_fsm_continuation(sess.id)")


def test_governor_defer_stays_on_cadence():
    """Ang rail-governor defer ay clock-bound (bucket refill) — ang instant
    retry ay hot spin; dapat WALANG continuation doon."""
    src = _tick_src()
    governor_at = src.index('if res.get("deferred"):')
    end_at = src.index("rail_governor_deferred", governor_at)
    seg = src[governor_at:end_at + 200]
    assert "_schedule_entry_fsm_continuation" not in seg


def test_candidate_and_pending_transitions_still_continue():
    """Ang mga umiiral na continuation sa candidate/pending transitions ay buo."""
    src = _tick_src()
    assert src.count("_schedule_entry_fsm_continuation(sess.id)") >= 4
