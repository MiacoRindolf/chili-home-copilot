"""P3 — PDT soft-warn (no blocking).

* Monitor stop/target auto-exit stamps an ``AutoTraderRun`` row with
  ``rule_snapshot.would_be_day_trade`` reflecting whether the position was
  opened on the current US/Eastern calendar day.
* Desk listing exposes ``opened_today_et`` per row and never blocks an exit.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.trading import AutoTraderRun, Trade
from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor
from app.services.trading.autotrader_desk import list_pattern_linked_open_positions


def _mk_live_trade(
    db: Session,
    user_id: int,
    *,
    ticker: str,
    entry_date: datetime,
    stop_loss: float = 9.5,
    take_profit: float = 12.0,
) -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=10.0,
        entry_date=entry_date,
        status="open",
        stop_loss=stop_loss,
        take_profit=take_profit,
        auto_trader_version="v1",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _fake_adapter(fill_price: float = 9.0) -> MagicMock:
    a = MagicMock()
    a.is_enabled.return_value = True
    a.place_market_order.return_value = {
        "ok": True,
        "order_id": "rh-1",
        "raw": {"average_price": str(fill_price)},
    }
    a.get_quote_price.return_value = None
    return a


def test_monitor_stamps_would_be_day_trade_on_same_day_exit(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_rth_only", False)

    t = _mk_live_trade(db, user.id, ticker="SAME1", entry_date=datetime.utcnow())
    adapter = _fake_adapter(fill_price=9.0)

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=adapter,
    ), patch(
        "app.services.trading.auto_trader_monitor._quote_price", return_value=8.0
    ):
        summary = tick_auto_trader_monitor(db)

    assert summary.get("closed", 0) == 1
    db.refresh(t)
    assert t.status == "closed"

    audit = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.trade_id == t.id, AutoTraderRun.decision == "monitor_exit")
        .first()
    )
    assert audit is not None
    snap = dict(audit.rule_snapshot or {})
    assert snap.get("opened_today_et") is True
    assert snap.get("would_be_day_trade") is True
    assert snap.get("exit_reason") == "stop"
    assert int(t.id) in summary.get("would_be_day_trade_exits", [])


def test_monitor_does_not_stamp_when_entered_yesterday(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_rth_only", False)

    yesterday = datetime.utcnow() - timedelta(days=2)
    t = _mk_live_trade(db, user.id, ticker="PRIOR1", entry_date=yesterday)
    adapter = _fake_adapter(fill_price=9.0)

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=adapter,
    ), patch(
        "app.services.trading.auto_trader_monitor._quote_price", return_value=8.0
    ):
        summary = tick_auto_trader_monitor(db)

    assert summary.get("closed", 0) == 1
    audit = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.trade_id == t.id, AutoTraderRun.decision == "monitor_exit")
        .first()
    )
    assert audit is not None
    snap = dict(audit.rule_snapshot or {})
    assert snap.get("opened_today_et") is False
    assert snap.get("would_be_day_trade") is False
    assert "would_be_day_trade_exits" not in summary or t.id not in summary.get("would_be_day_trade_exits", [])


def test_desk_exposes_opened_today_et(paired_client, db: Session) -> None:
    _c, user = paired_client
    t_new = _mk_live_trade(db, user.id, ticker="TODAY", entry_date=datetime.utcnow())
    t_old = _mk_live_trade(
        db, user.id, ticker="PRIOR", entry_date=datetime.utcnow() - timedelta(days=3)
    )
    # Desk listing requires a pattern link
    t_new.related_alert_id = None
    t_new.scan_pattern_id = None
    # Force one into the desk listing with a dummy scan_pattern_id via indicator_snapshot — the service
    # requires scan_pattern_id or related_alert_id; add a dummy link so both are listed.
    from app.models.trading import ScanPattern

    sp = ScanPattern(
        name="pdt_sp",
        rules_json={},
        origin="user",
        asset_class="stock",
        timeframe="1d",
    )
    db.add(sp)
    db.flush()
    t_new.scan_pattern_id = sp.id
    t_old.scan_pattern_id = sp.id
    db.commit()

    data = list_pattern_linked_open_positions(db, user.id)
    by_ticker = {r["ticker"]: r for r in data["trades"]}
    assert by_ticker["TODAY"]["opened_today_et"] is True
    assert by_ticker["PRIOR"]["opened_today_et"] is False
