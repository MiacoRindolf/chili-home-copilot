"""P2 — per-position Autopilot overrides (monitor pause, synergy exclude, close-now).

Exercises:

* ``get/set/list_position_overrides`` against ``BrainRuntimeMode``.
* ``auto_trader_monitor.tick_auto_trader_monitor`` respects ``monitor_paused``
  for both live and paper rows.
* ``auto_trader_synergy.maybe_scale_in`` returns ``None`` when the existing
  trade is flagged ``synergy_excluded``.
* ``close_position_now`` — live path (mocked RH adapter) and paper path
  (real close with slippage + audit row).
* API: PATCH overrides, POST close, guest forbidden, ``confirm=true`` required,
  unsupported ``kind`` rejected.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models.trading import (
    AutoTraderRun,
    BrainRuntimeMode,
    BreakoutAlert,
    PaperTrade,
    ScanPattern,
    Trade,
)
from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor
from app.services.trading.auto_trader_position_overrides import (
    _slice_name,
    close_position_now,
    get_position_overrides,
    list_position_overrides,
    paused_paper_trade_ids_for_user,
    set_position_override,
)
from app.services.trading.auto_trader_synergy import maybe_scale_in


# ───────────────────────────── service layer ──────────────────────────────


def test_overrides_roundtrip(db: Session) -> None:
    assert get_position_overrides(db, "trade", 42) == {
        "monitor_paused": False,
        "synergy_excluded": False,
    }

    set_position_override(db, "trade", 42, "monitor_paused", True)
    got = get_position_overrides(db, "trade", 42)
    assert got == {"monitor_paused": True, "synergy_excluded": False}

    set_position_override(db, "trade", 42, "synergy_excluded", True)
    got = get_position_overrides(db, "trade", 42)
    assert got == {"monitor_paused": True, "synergy_excluded": True}

    row = db.query(BrainRuntimeMode).filter(
        BrainRuntimeMode.slice_name == _slice_name("trade", 42)
    ).first()
    assert row is not None
    assert row.payload_json.get("kind") == "trade"
    assert row.payload_json.get("monitor_paused") is True
    assert row.payload_json.get("synergy_excluded") is True


def test_overrides_invalid_field_raises(db: Session) -> None:
    with pytest.raises(ValueError):
        set_position_override(db, "trade", 1, "bad_field", True)


def test_list_position_overrides_bulk(db: Session) -> None:
    set_position_override(db, "trade", 1, "monitor_paused", True)
    set_position_override(db, "paper", 9, "synergy_excluded", True)
    bulk = list_position_overrides(
        db, [("trade", 1), ("trade", 2), ("paper", 9)]
    )
    assert bulk[("trade", 1)]["monitor_paused"] is True
    assert bulk[("trade", 2)] == {"monitor_paused": False, "synergy_excluded": False}
    assert bulk[("paper", 9)]["synergy_excluded"] is True


# ───────────────────────── monitor pause (live) ───────────────────────────


def _mk_autotrader_trade(db: Session, user_id: int, ticker: str = "PAUS") -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=10.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.5,
        take_profit=12.0,
        scan_pattern_id=None,
        related_alert_id=None,
        auto_trader_version="v1",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_monitor_skips_live_trade_when_monitor_paused(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_rth_only", False)
    monkeypatch.setattr(_s, "chili_autotrader_user_id", int(user.id))

    t = _mk_autotrader_trade(db, user.id, "PAUS")
    # Pause monitor
    set_position_override(db, "trade", int(t.id), "monitor_paused", True)

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.return_value = {"ok": True, "raw": {}}

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ), patch(
        "app.services.trading.auto_trader_monitor._quote_price",
        return_value=8.0,  # below stop — would normally sell
    ):
        summary = tick_auto_trader_monitor(db)

    db.refresh(t)
    assert t.status == "open", "paused monitor should not sell"
    assert summary.get("closed", 0) == 0
    assert int(t.id) in summary.get("live_monitor_paused_ids", [])
    fake_adapter.place_market_order.assert_not_called()


# ───────────────────────── paper pause path ───────────────────────────────


def _mk_autotrader_paper(db: Session, user_id: int, ticker: str = "PPAU") -> PaperTrade:
    pt = PaperTrade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=10,
        status="open",
        stop_price=9.5,
        target_price=12.0,
        scan_pattern_id=None,
        signal_json={"auto_trader_v1": True, "breakout_alert_id": 0},
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    return pt


def test_paused_paper_trade_ids_for_user(paired_client, db: Session) -> None:
    _c, user = paired_client
    pt1 = _mk_autotrader_paper(db, user.id, "PX1")
    pt2 = _mk_autotrader_paper(db, user.id, "PX2")

    set_position_override(db, "paper", int(pt1.id), "monitor_paused", True)

    paused = paused_paper_trade_ids_for_user(db, user.id)
    assert pt1.id in paused
    assert pt2.id not in paused


# ───────────────────────── synergy exclude ────────────────────────────────


def test_maybe_scale_in_blocked_when_synergy_excluded(
    paired_client, db: Session
) -> None:
    _c, user = paired_client
    sp_a = ScanPattern(
        name="sp_a", rules_json={}, origin="user", asset_class="stock", timeframe="1d"
    )
    sp_b = ScanPattern(
        name="sp_b", rules_json={}, origin="user", asset_class="stock", timeframe="1d"
    )
    db.add_all([sp_a, sp_b])
    db.flush()

    t = _mk_autotrader_trade(db, user.id, "SYN1")
    t.scan_pattern_id = sp_a.id
    db.commit()

    set_position_override(db, "trade", int(t.id), "synergy_excluded", True)

    class _S:
        chili_autotrader_synergy_enabled = True
        chili_autotrader_per_trade_notional_usd = 300.0
        chili_autotrader_synergy_scale_notional_usd = 150.0

    plan = maybe_scale_in(
        db,
        user_id=user.id,
        ticker="SYN1",
        new_scan_pattern_id=sp_b.id,
        new_stop=9.6,
        new_target=13.0,
        current_price=10.5,
        settings=_S(),
    )
    assert plan is None


def test_maybe_scale_in_allowed_when_flag_cleared(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp_a = ScanPattern(
        name="sp_a2", rules_json={}, origin="user", asset_class="stock", timeframe="1d"
    )
    sp_b = ScanPattern(
        name="sp_b2", rules_json={}, origin="user", asset_class="stock", timeframe="1d"
    )
    db.add_all([sp_a, sp_b])
    db.flush()

    t = _mk_autotrader_trade(db, user.id, "SYN2")
    t.scan_pattern_id = sp_a.id
    db.commit()

    set_position_override(db, "trade", int(t.id), "synergy_excluded", False)

    class _S:
        chili_autotrader_synergy_enabled = True
        chili_autotrader_per_trade_notional_usd = 300.0
        chili_autotrader_synergy_scale_notional_usd = 150.0

    plan = maybe_scale_in(
        db,
        user_id=user.id,
        ticker="SYN2",
        new_scan_pattern_id=sp_b.id,
        new_stop=9.6,
        new_target=13.0,
        current_price=10.5,
        settings=_S(),
    )
    assert plan is not None


# ───────────────────────── close-now paper path ───────────────────────────


def test_close_position_now_paper(paired_client, db: Session) -> None:
    _c, user = paired_client
    pt = _mk_autotrader_paper(db, user.id, "CLP1")

    with patch(
        "app.services.trading.auto_trader_position_overrides._current_quote_price",
        return_value=11.0,
    ):
        res = close_position_now(db, kind="paper", trade_id=int(pt.id))

    assert res["ok"] is True
    db.refresh(pt)
    assert pt.status == "closed"
    assert pt.exit_reason == "desk_close_now"
    audit = (
        db.query(AutoTraderRun)
        .filter(AutoTraderRun.decision == "desk_close_now")
        .first()
    )
    assert audit is not None
    assert audit.ticker == "CLP1"


# ───────────────────────── close-now live path ────────────────────────────


def test_close_position_now_live(paired_client, db: Session) -> None:
    _c, user = paired_client
    t = _mk_autotrader_trade(db, user.id, "CLT1")

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.return_value = {
        "ok": True,
        "order_id": "rh-42",
        "state": "filled",
        "raw": {"average_price": "11.25", "state": "filled"},
    }
    fake_adapter.get_product.return_value = ({"market_hours_mic": "XNAS", "tradable": True, "tick_size": 0.01}, False)

    rth_window = {
        "ticker": "CLT1",
        "session": "regular_hours",
        "session_label": "Regular session",
        "market_hours": "regular_hours",
        "next_eligible_session_at": None,
        "overnight_eligible": False,
        "can_submit_now": True,
        "execution_reason": "Regular session",
    }
    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ), patch(
        "app.services.trading.robinhood_exit_execution.describe_robinhood_equity_execution_window",
        return_value=rth_window,
    ):
        res = close_position_now(db, kind="trade", trade_id=int(t.id))

    assert res["ok"] is True
    db.refresh(t)
    assert t.status == "closed"
    assert t.exit_reason == "desk_close_now"
    assert abs(float(t.exit_price) - 11.25) < 1e-6
    fake_adapter.place_market_order.assert_called_once()


def test_close_position_now_live_plan_levels(paired_client, db: Session) -> None:
    _c, user = paired_client
    t = Trade(
        user_id=user.id,
        ticker="CLP2",
        direction="long",
        entry_price=10.0,
        quantity=4.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=9.25,
        take_profit=11.5,
        broker_source="robinhood",
        tags="robinhood-sync",
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.return_value = {
        "ok": True,
        "order_id": "rh-plan-42",
        "state": "filled",
        "raw": {"average_price": "9.10", "state": "filled"},
    }
    fake_adapter.get_product.return_value = ({"market_hours_mic": "XNAS", "tradable": True, "tick_size": 0.01}, False)

    rth_window = {
        "ticker": "CLP2",
        "session": "regular_hours",
        "session_label": "Regular session",
        "market_hours": "regular_hours",
        "next_eligible_session_at": None,
        "overnight_eligible": False,
        "can_submit_now": True,
        "execution_reason": "Regular session",
    }
    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ), patch(
        "app.services.trading.robinhood_exit_execution.describe_robinhood_equity_execution_window",
        return_value=rth_window,
    ):
        res = close_position_now(db, kind="trade", trade_id=int(t.id))

    assert res["ok"] is True
    db.refresh(t)
    assert t.status == "closed"
    assert t.exit_reason == "desk_close_now"
    assert abs(float(t.exit_price) - 9.10) < 1e-6
    fake_adapter.place_market_order.assert_called_once()


def test_close_position_now_live_rh_off(paired_client, db: Session) -> None:
    _c, user = paired_client
    t = _mk_autotrader_trade(db, user.id, "CLT2")

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = False

    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ):
        res = close_position_now(db, kind="trade", trade_id=int(t.id))

    assert res["ok"] is False
    assert res["error"] == "rh_adapter_off"
    fake_adapter.place_market_order.assert_not_called()


# ───────────────────────────── API layer ──────────────────────────────────


def test_api_patch_override_paired(paired_client, db: Session) -> None:
    c, user = paired_client
    t = _mk_autotrader_trade(db, user.id, "APPAT")

    r = c.patch(
        f"/api/trading/autotrader/positions/{t.id}",
        json={"kind": "trade", "monitor_paused": True, "synergy_excluded": True},
    )
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["ok"] is True
    assert js["overrides"] == {"monitor_paused": True, "synergy_excluded": True}
    assert set(js["updated"]) == {"monitor_paused", "synergy_excluded"}


def test_api_patch_override_guest_forbidden(client) -> None:
    r = client.patch(
        "/api/trading/autotrader/positions/1",
        json={"kind": "trade", "monitor_paused": True},
    )
    assert r.status_code == 403


def test_api_patch_override_bad_kind(paired_client) -> None:
    c, _u = paired_client
    r = c.patch(
        "/api/trading/autotrader/positions/1",
        json={"kind": "live_futures", "monitor_paused": True},
    )
    assert r.status_code == 400


def test_api_close_requires_confirm(paired_client, db: Session) -> None:
    c, user = paired_client
    t = _mk_autotrader_trade(db, user.id, "APCNF")
    r = c.post(
        f"/api/trading/autotrader/positions/{t.id}/close",
        json={"kind": "trade"},  # no confirm
    )
    assert r.status_code == 400


def test_api_close_paper(paired_client, db: Session) -> None:
    c, user = paired_client
    pt = _mk_autotrader_paper(db, user.id, "APCLP")
    with patch(
        "app.services.trading.auto_trader_position_overrides._current_quote_price",
        return_value=10.75,
    ):
        r = c.post(
            f"/api/trading/autotrader/positions/{pt.id}/close",
            json={"kind": "paper", "confirm": True},
        )
    assert r.status_code == 200
    db.refresh(pt)
    assert pt.status == "closed"
