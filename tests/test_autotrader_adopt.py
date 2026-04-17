"""Adopt / Unadopt pattern-linked positions into/out of AutoTrader v1.

Cover:

* ``adopt_position_into_v1`` — live + paper: flips the version flag, seeds
  stop/target from the body or from the linked ``ScanPattern`` exit hints,
  writes an ``AutoTraderRun(decision="adopt_manual")`` audit row.
* ``unadopt_position_from_v1`` — clears the flag, clears per-position overrides,
  writes an ``unadopt_manual`` audit row; position stays open.
* Close-now path works for **non-v1** pattern-linked positions (Option A).
* API: ``POST /adopt`` / ``POST /unadopt`` — paired required, bad kind rejected,
  confirm required on unadopt.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.models.trading import (
    AutoTraderRun,
    BrainRuntimeMode,
    PaperTrade,
    ScanPattern,
    Trade,
)
from app.services.trading.auto_trader_position_overrides import (
    _slice_name,
    adopt_position_into_v1,
    close_position_now,
    set_position_override,
    unadopt_position_from_v1,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _mk_pattern(db: Session, name: str = "adopt_pat", **extra_rules) -> ScanPattern:
    rj = {"exits": {"stop_pct": 5.0, "target_pct": 10.0}} if not extra_rules else extra_rules
    sp = ScanPattern(
        name=name,
        rules_json=rj,
        origin="user",
        asset_class="stock",
        timeframe="1d",
    )
    db.add(sp)
    db.flush()
    return sp


def _mk_linked_trade(
    db: Session,
    user_id: int,
    *,
    ticker: str = "ADOP",
    scan_pattern_id: int | None = None,
    stop: float | None = None,
    target: float | None = None,
    auto_trader_version: str | None = None,
) -> Trade:
    t = Trade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=5.0,
        entry_date=datetime.utcnow(),
        status="open",
        stop_loss=stop,
        take_profit=target,
        scan_pattern_id=scan_pattern_id,
        auto_trader_version=auto_trader_version,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _mk_linked_paper(
    db: Session,
    user_id: int,
    *,
    ticker: str = "PADO",
    scan_pattern_id: int | None = None,
    stop: float | None = None,
    target: float | None = None,
    atv1: bool = False,
) -> PaperTrade:
    pt = PaperTrade(
        user_id=user_id,
        ticker=ticker,
        direction="long",
        entry_price=10.0,
        quantity=10,
        status="open",
        stop_price=stop,
        target_price=target,
        scan_pattern_id=scan_pattern_id,
        signal_json={"auto_trader_v1": bool(atv1)} if atv1 else {},
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    return pt


# ── Service: adopt (live) ────────────────────────────────────────────────


def test_adopt_trade_sets_version_and_writes_audit(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(db, user.id, ticker="ADTV", scan_pattern_id=sp.id)

    res = adopt_position_into_v1(
        db, kind="trade", trade_id=int(t.id), stop=9.0, target=12.5
    )

    assert res["ok"] is True
    db.refresh(t)
    assert t.auto_trader_version == "v1"
    assert abs(float(t.stop_loss) - 9.0) < 1e-6
    assert abs(float(t.take_profit) - 12.5) < 1e-6

    audit = (
        db.query(AutoTraderRun)
        .filter(
            AutoTraderRun.trade_id == t.id,
            AutoTraderRun.decision == "adopt_manual",
        )
        .first()
    )
    assert audit is not None
    snap = dict(audit.rule_snapshot or {})
    assert snap.get("stop_loss") == 9.0
    assert snap.get("take_profit") == 12.5


def test_adopt_trade_seeds_from_pattern_when_body_empty(
    paired_client, db: Session
) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db, exits={"stop_pct": 5.0, "target_pct": 10.0})
    t = _mk_linked_trade(db, user.id, ticker="ADTS", scan_pattern_id=sp.id)

    res = adopt_position_into_v1(db, kind="trade", trade_id=int(t.id))

    assert res["ok"] is True
    db.refresh(t)
    # entry=10.0, stop_pct=5 -> 9.5, target_pct=10 -> 11.0
    assert abs(float(t.stop_loss) - 9.5) < 1e-6
    assert abs(float(t.take_profit) - 11.0) < 1e-6


def test_adopt_trade_rejects_already_v1(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(
        db, user.id, ticker="ADTR", scan_pattern_id=sp.id, auto_trader_version="v1"
    )

    res = adopt_position_into_v1(db, kind="trade", trade_id=int(t.id))
    assert res["ok"] is False
    assert res["error"] == "already_v1"


def test_adopt_trade_rejects_unlinked(paired_client, db: Session) -> None:
    _c, user = paired_client
    t = _mk_linked_trade(db, user.id, ticker="NOLN", scan_pattern_id=None)

    res = adopt_position_into_v1(db, kind="trade", trade_id=int(t.id))
    assert res["ok"] is False
    assert res["error"] == "not_pattern_linked"


# ── Service: adopt (paper) ───────────────────────────────────────────────


def test_adopt_paper_flips_signal_json(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    pt = _mk_linked_paper(db, user.id, ticker="ADPP", scan_pattern_id=sp.id)

    res = adopt_position_into_v1(
        db, kind="paper", trade_id=int(pt.id), stop=9.1, target=11.9
    )

    assert res["ok"] is True
    db.refresh(pt)
    assert (pt.signal_json or {}).get("auto_trader_v1") is True
    assert abs(float(pt.stop_price) - 9.1) < 1e-6
    assert abs(float(pt.target_price) - 11.9) < 1e-6


def test_adopt_paper_rejects_already_v1(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    pt = _mk_linked_paper(db, user.id, ticker="ADPA", scan_pattern_id=sp.id, atv1=True)

    res = adopt_position_into_v1(db, kind="paper", trade_id=int(pt.id))
    assert res["ok"] is False
    assert res["error"] == "already_v1"


# ── Service: unadopt ─────────────────────────────────────────────────────


def test_unadopt_trade_clears_flag_and_overrides(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(
        db, user.id, ticker="UNAT", scan_pattern_id=sp.id, auto_trader_version="v1"
    )
    set_position_override(db, "trade", int(t.id), "monitor_paused", True)
    assert (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == _slice_name("trade", int(t.id)))
        .first()
        is not None
    )

    res = unadopt_position_from_v1(db, kind="trade", trade_id=int(t.id))
    assert res["ok"] is True
    db.refresh(t)
    assert (t.auto_trader_version or "") != "v1"
    # Override row was cleared.
    assert (
        db.query(BrainRuntimeMode)
        .filter(BrainRuntimeMode.slice_name == _slice_name("trade", int(t.id)))
        .first()
        is None
    )
    audit = (
        db.query(AutoTraderRun)
        .filter(
            AutoTraderRun.trade_id == t.id,
            AutoTraderRun.decision == "unadopt_manual",
        )
        .first()
    )
    assert audit is not None


def test_unadopt_paper_flips_signal_json(paired_client, db: Session) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    pt = _mk_linked_paper(db, user.id, ticker="UNPA", scan_pattern_id=sp.id, atv1=True)

    res = unadopt_position_from_v1(db, kind="paper", trade_id=int(pt.id))
    assert res["ok"] is True
    db.refresh(pt)
    assert (pt.signal_json or {}).get("auto_trader_v1") is False


# ── Close-now on non-v1 linked positions (Option A) ──────────────────────


def test_close_now_allowed_on_non_v1_linked_live(
    paired_client, db: Session
) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(
        db, user.id, ticker="CNLN", scan_pattern_id=sp.id, auto_trader_version=None
    )

    fake_adapter = MagicMock()
    fake_adapter.is_enabled.return_value = True
    fake_adapter.place_market_order.return_value = {
        "ok": True,
        "order_id": "rh-close-nonv1",
        "raw": {"average_price": "10.75"},
    }
    with patch(
        "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
        return_value=fake_adapter,
    ):
        res = close_position_now(db, kind="trade", trade_id=int(t.id))

    assert res["ok"] is True
    db.refresh(t)
    assert t.status == "closed"
    assert t.exit_reason == "desk_close_now"
    fake_adapter.place_market_order.assert_called_once()


def test_close_now_allowed_on_non_v1_linked_paper(
    paired_client, db: Session
) -> None:
    _c, user = paired_client
    sp = _mk_pattern(db)
    pt = _mk_linked_paper(db, user.id, ticker="CNPA", scan_pattern_id=sp.id, atv1=False)

    with patch(
        "app.services.trading.auto_trader_position_overrides._current_quote_price",
        return_value=11.5,
    ):
        res = close_position_now(db, kind="paper", trade_id=int(pt.id))

    assert res["ok"] is True
    db.refresh(pt)
    assert pt.status == "closed"
    assert pt.exit_reason == "desk_close_now"


def test_close_now_rejects_unlinked_trade(paired_client, db: Session) -> None:
    _c, user = paired_client
    t = _mk_linked_trade(db, user.id, ticker="ORPH", scan_pattern_id=None)
    res = close_position_now(db, kind="trade", trade_id=int(t.id))
    assert res["ok"] is False
    assert res["error"] == "not_pattern_linked"


# ── API ──────────────────────────────────────────────────────────────────


def test_api_adopt_paired(paired_client, db: Session) -> None:
    c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(db, user.id, ticker="APAD", scan_pattern_id=sp.id)

    r = c.post(
        f"/api/trading/autotrader/positions/{t.id}/adopt",
        json={"kind": "trade", "stop": 8.9, "target": 13.0},
    )
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["ok"] is True
    assert abs(float(js["stop"]) - 8.9) < 1e-6
    assert abs(float(js["target"]) - 13.0) < 1e-6
    db.refresh(t)
    assert t.auto_trader_version == "v1"


def test_api_adopt_bad_kind(paired_client, db: Session) -> None:
    c, _u = paired_client
    r = c.post(
        "/api/trading/autotrader/positions/1/adopt",
        json={"kind": "options"},
    )
    assert r.status_code == 400


def test_api_adopt_guest_forbidden(client) -> None:
    r = client.post(
        "/api/trading/autotrader/positions/1/adopt",
        json={"kind": "trade"},
    )
    assert r.status_code == 403


def test_api_unadopt_requires_confirm(paired_client, db: Session) -> None:
    c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(
        db, user.id, ticker="APUC", scan_pattern_id=sp.id, auto_trader_version="v1"
    )
    r = c.post(
        f"/api/trading/autotrader/positions/{t.id}/unadopt",
        json={"kind": "trade"},
    )
    assert r.status_code == 400


def test_api_unadopt_paired(paired_client, db: Session) -> None:
    c, user = paired_client
    sp = _mk_pattern(db)
    t = _mk_linked_trade(
        db, user.id, ticker="APUP", scan_pattern_id=sp.id, auto_trader_version="v1"
    )
    r = c.post(
        f"/api/trading/autotrader/positions/{t.id}/unadopt",
        json={"kind": "trade", "confirm": True},
    )
    assert r.status_code == 200, r.text
    db.refresh(t)
    assert (t.auto_trader_version or "") != "v1"
