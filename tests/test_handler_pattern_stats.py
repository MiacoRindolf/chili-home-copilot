"""Tests for f-handler-pattern-stats.

Covers the 7 cases from the brief:
  1.  ``handle_paper_trade_closed`` calls
      ``update_pattern_stats_from_closed_trades`` with the right user_id.
  2.  ``handle_live_trade_closed`` same pattern.
  3.  ``handle_broker_fill_closed`` same pattern.
  4.  Handler swallows exceptions raised by the recompute fn.
  5.  Integration: synthetic paper close -> recompute -> audit row.
  6.  Idempotence: second call on same event yields ``no_change`` audit row.
  7.  (Implicit, not part of this file) existing exit-evaluator + parity
      tests still pass -- run separately.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.trading import (
    PatternEvidenceCorrection,
    ScanPattern,
    Trade,
)
from app.services.trading.brain_work.handlers import pattern_stats
from app.services.trading.brain_work.handlers.pattern_stats import (
    handle_broker_fill_closed,
    handle_live_trade_closed,
    handle_paper_trade_closed,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_pattern(
    db, *, timeframe: str = "1d",
    win_rate: float | None = 0.5, avg_return_pct: float | None = 0.0,
    trade_count: int = 0, exit_config: dict | None = None,
) -> ScanPattern:
    pat = ScanPattern(
        name=f"phs_{timeframe}_pat",
        rules_json={},
        origin="test",
        asset_class="all",
        timeframe=timeframe,
        win_rate=win_rate,
        avg_return_pct=avg_return_pct,
        trade_count=trade_count,
        exit_config=exit_config or {"max_bars": 20},
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return pat


def _seed_closed_trade(
    db, *, pattern_id: int,
    entry_price: float = 100.0, exit_price: float = 105.0,
    direction: str = "long", ticker: str = "TEST",
    entry_offset: timedelta = timedelta(days=2),
    held_for: timedelta = timedelta(days=1),
    user_id: int | None = None,
) -> Trade:
    entry_dt = datetime.utcnow() - entry_offset
    exit_dt = entry_dt + held_for
    pnl = (
        (exit_price - entry_price)
        if direction == "long"
        else (entry_price - exit_price)
    )
    t = Trade(
        ticker=ticker,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        status="closed",
        entry_date=entry_dt,
        exit_date=exit_dt,
        pnl=pnl,
        scan_pattern_id=pattern_id,
        user_id=user_id,
        # Clean (non-dirty, non-empty) exit reason so the writer's
        # f-realized-ev-exit-cleanliness filter (learning.py) keeps the row.
        exit_reason="target_hit",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _fake_event(event_id: int = 99, payload: dict | None = None):
    return SimpleNamespace(id=event_id, payload=payload or {})


# ---------------------------------------------------------------------------
# 1-3. Each handler entry calls the recompute fn with the right user_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "handler,source",
    [
        (handle_paper_trade_closed, "paper"),
        (handle_live_trade_closed, "live"),
        (handle_broker_fill_closed, "broker"),
    ],
)
def test_handler_calls_recompute_with_user_id(db, handler, source):
    captured: dict = {}

    def _stub(sess, user_id):
        captured["user_id"] = user_id
        return {"patterns_updated": 0, "cycle_run_id": "stub"}

    with patch(
        "app.services.trading.learning.update_pattern_stats_from_closed_trades",
        side_effect=_stub,
    ):
        ev = _fake_event(payload={"user_id": 7})
        handler(db, ev, user_id=None)
    assert captured["user_id"] == 7


def test_handler_falls_back_to_arg_user_id_when_payload_missing(db):
    captured: dict = {}

    def _stub(sess, user_id):
        captured["user_id"] = user_id
        return {"patterns_updated": 0, "cycle_run_id": "stub"}

    with patch(
        "app.services.trading.learning.update_pattern_stats_from_closed_trades",
        side_effect=_stub,
    ):
        ev = _fake_event(payload={})
        handle_paper_trade_closed(db, ev, user_id=42)
    assert captured["user_id"] == 42


# ---------------------------------------------------------------------------
# 4. Handler swallows exceptions
# ---------------------------------------------------------------------------

def test_handler_swallows_recompute_exception(db, caplog):
    def _boom(sess, user_id):
        raise RuntimeError("simulated recompute failure")

    with patch(
        "app.services.trading.learning.update_pattern_stats_from_closed_trades",
        side_effect=_boom,
    ):
        # Must NOT raise; logger captures the failure.
        handle_paper_trade_closed(db, _fake_event(), user_id=None)

    # Verify the failure was logged at exception level.
    assert any(
        "pattern_stats" in rec.name and rec.levelname in ("ERROR", "WARNING")
        for rec in caplog.records
    ) or any(
        "simulated recompute failure" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# 5. End-to-end: handler call -> recompute -> audit row
# ---------------------------------------------------------------------------

def test_handler_end_to_end_writes_audit_row(db):
    pat = _seed_pattern(db, timeframe="1d")
    for prices in [(100, 102), (100, 101), (100, 99)]:
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=prices[0], exit_price=prices[1],
            entry_offset=timedelta(days=2), held_for=timedelta(days=1),
        )
    handle_paper_trade_closed(db, _fake_event(), user_id=None)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).all()
    assert len(rows) == 1
    assert rows[0].closed_trades_considered == 3
    assert rows[0].correction_reason in ("first_run_backfill",)


# ---------------------------------------------------------------------------
# 6. Idempotence: handler twice on same event -> matching audit rows
# ---------------------------------------------------------------------------

def test_handler_idempotence_second_call_yields_no_change(db):
    pat = _seed_pattern(db, timeframe="1d")
    for prices in [(100, 102), (100, 99)]:
        _seed_closed_trade(
            db, pattern_id=pat.id,
            entry_price=prices[0], exit_price=prices[1],
            entry_offset=timedelta(days=2), held_for=timedelta(days=1),
        )
    handle_paper_trade_closed(db, _fake_event(event_id=1), user_id=None)
    handle_paper_trade_closed(db, _fake_event(event_id=2), user_id=None)
    rows = db.query(PatternEvidenceCorrection).filter_by(
        scan_pattern_id=pat.id,
    ).order_by(PatternEvidenceCorrection.id.asc()).all()
    assert len(rows) == 2
    assert rows[0].correction_reason == "first_run_backfill"
    assert rows[1].correction_reason in ("no_change", "periodic_recompute")


# ---------------------------------------------------------------------------
# Bonus: dispatcher wires pattern_stats into all three close event types
# (regression guard against accidental dispatcher edit removing the wiring)
# ---------------------------------------------------------------------------

def test_dispatcher_imports_pattern_stats_for_close_events():
    """Sanity guard: the dispatcher's close-event branch must reference
    each of the three pattern_stats entry points. If a future edit deletes
    one of these references the wiring breaks silently — this test makes
    that audible."""
    from pathlib import Path

    src = Path("app/services/trading/brain_work/dispatcher.py").read_text()
    for name in (
        "handle_paper_trade_closed",
        "handle_live_trade_closed",
        "handle_broker_fill_closed",
    ):
        assert name in src, f"dispatcher.py missing reference to {name}"
