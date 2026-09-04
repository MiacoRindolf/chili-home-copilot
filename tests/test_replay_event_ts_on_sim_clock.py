"""FSM events must sit on the same clock as the fills they explain.

MEASURED 2026-09-04 on the first Ross-bench receipt that carried real decisions (SDOT
2026-06-26, robinhood_agentic_mcp: 4,190 events, 7 fills, +$3.61): every FILL was stamped
with the sim instant (2026-06-26T13:18:12) and every EVENT with the wall clock
(2026-09-04 20:17:57). The step-10 timeline could place none of them inside the window and
the scorer graded the case ``unknown(no_replay_events)`` -- a receipt full of decisions that
read as silence. ``append_trading_automation_event`` stamped ``datetime.utcnow()`` directly,
bypassing the ``_utcnow`` chokepoint that ``replay_clock`` freezes.

Production is byte-identical: outside ``replay_clock`` the chokepoint IS ``datetime.utcnow()``.
The sim clock is opted into by the runner's ``_emit``; every other writer keeps the wall clock.

DB: uses the ``db`` fixture (``_test``-suffixed sink, truncated per test).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import TradingAutomationSession
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.persistence import append_trading_automation_event


SIM_INSTANT = datetime(2026, 6, 26, 13, 18, 12)


def _seed(db):
    arm = rv3.RecordedArm(
        symbol="SDOT",
        live_eligible_at_utc="2026-06-26T13:05:00",
        viability_score=0.9,
        atr_pct=0.05,
    )
    seed = rv3.seed_replay_session(db, arm=arm, execution_family="robinhood_agentic_mcp")
    return db.get(TradingAutomationSession, seed.session_id)


def test_emit_under_replay_clock_lands_on_the_sim_instant(db):
    sess = _seed(db)
    with lr.replay_clock(SIM_INSTANT):
        ev = lr._emit(db, sess, "live_entry_candidate", {"reason": "test"})
    assert ev.ts == SIM_INSTANT


def test_emit_outside_replay_clock_is_the_wall_clock(db):
    sess = _seed(db)
    before = datetime.utcnow() - timedelta(seconds=2)
    ev = lr._emit(db, sess, "live_watch_started", {})
    assert before <= ev.ts <= datetime.utcnow() + timedelta(seconds=2)


def test_every_other_writer_keeps_the_wall_clock_even_inside_a_frozen_clock(db):
    """No ``ts`` passed -> wall clock, even inside ``replay_clock``: operator/ops writers
    are never silently moved onto a sim timeline."""
    sess = _seed(db)
    before = datetime.utcnow() - timedelta(seconds=2)
    with lr.replay_clock(SIM_INSTANT):
        ev = append_trading_automation_event(db, sess.id, "operator_note", {})
    assert ev.ts != SIM_INSTANT
    assert ev.ts >= before
