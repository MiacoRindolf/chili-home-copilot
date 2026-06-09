"""Marketable-LIMIT entry (Ross-style, sweep-protected) — the momentum live runner
places the entry as a marketable limit capped at the guarded ask, NOT a market order
that can sweep a thin low-float book. Root cause: 0 clean equity fills ever; the live
gate correctly refused market-order entries into 4.6%-avg spreads (project memory
project_momentum_zero_fills_root_cause)."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy.orm import Session

from app.config import settings
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_PENDING_ENTRY,
    STATE_WATCHING_LIVE,
)
from app.services.trading.momentum_neural.live_runner import (
    _fmt_limit_price_buy,
    tick_live_session,
)
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.services.trading.venue.coinbase_spot import reset_duplicate_client_order_guard_for_tests

from tests.test_momentum_live_runner import _fresh, _mk_adapter, _uid
from tests.test_momentum_paper_runner import _seed_live_eligible_row


# ── pure: the buy-limit price formatter ──────────────────────────────────────
def test_fmt_limit_price_buy_equity_rounds_up_to_penny() -> None:
    # >= $1: penny tick, rounded UP so a marketable buy stays marketable.
    assert _fmt_limit_price_buy(2.2155) == "2.22"
    assert _fmt_limit_price_buy(15.031) == "15.04"
    assert _fmt_limit_price_buy(5.0) == "5.00"       # already on the tick
    assert _fmt_limit_price_buy(1.0) == "1.00"


def test_fmt_limit_price_buy_subdollar_keeps_precision() -> None:
    # < $1 (crypto / penny names): finer precision for the venue to quantize.
    assert _fmt_limit_price_buy(0.12345678) == "0.12345678"
    assert _fmt_limit_price_buy(0.5) == "0.5"


def test_fmt_limit_price_buy_invalid_is_zero() -> None:
    assert _fmt_limit_price_buy(0.0) == "0"
    assert _fmt_limit_price_buy(-3.0) == "0"
    assert _fmt_limit_price_buy(float("nan")) == "0"
    assert _fmt_limit_price_buy(float("inf")) == "0"


def test_fmt_limit_price_buy_never_below_input_for_buy() -> None:
    # Rounding UP must never make the marketable buy LESS marketable.
    for px in (1.001, 2.349, 9.999, 14.872):
        assert float(_fmt_limit_price_buy(px)) >= px


# ── integration: the entry places a marketable LIMIT at the guarded ask ───────
def _mk_pending_entry_session(db: Session, symbol: str):
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, "limit_entry")
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_PENDING_ENTRY,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_live_execution": {"entry_submitted": False},
        },
    )
    db.commit()
    return sess


def test_entry_places_marketable_limit_not_market(monkeypatch, db: Session) -> None:
    reset_duplicate_client_order_guard_for_tests()
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # Don't require a brain decision packet for this unit (it's exercised elsewhere).
    monkeypatch.setattr(settings, "brain_decision_packet_required_for_runners", False)
    sess = _mk_pending_entry_session(db, "SOL-USD")
    ad = _mk_adapter()  # ask=100.05 -> guarded_ask 100.05*1.0025=100.30 (penny ceil)

    with patch(
        "app.services.trading.momentum_neural.live_runner.is_kill_switch_active",
        return_value=False,
    ):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit(); db.refresh(sess)

    # Entry must be a marketable LIMIT, never a market order (no thin-book sweep).
    ad.place_market_order.assert_not_called()
    ad.place_limit_order_gtc.assert_called_once()
    kw = ad.place_limit_order_gtc.call_args.kwargs
    assert kw["side"] == "buy"
    # capped at the guarded ask (ask + the notional-guard buffer), penny-ceil'd:
    # 100.05 * 1.0025 = 100.300125 -> ceil-penny -> 100.31
    assert kw["limit_price"] == "100.31", (out, kw)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("entry_order_type") == "limit"
    assert le.get("entry_limit_price") == "100.31"
