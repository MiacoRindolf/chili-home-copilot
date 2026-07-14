"""WAVE-4 ITEM-6 — VIABILITY FRESHNESS PAIR.

(a) SCHEDULER half: the equity viability refresh must re-score ACTIVE-session symbols even
    when the scanners emit no signal for them (a consolidating watched name rotates OUT of
    the mover list -> stale-while-watched -> the 600s freshness gate strangles the entry;
    DXST died at 537s/600s). ``_active_equity_session_symbols`` surfaces those names.

(b) CONFIRM half: at confirm_live_arm, a viability row already past HALF the max-age has
    < 0.5x the freshness budget left, so the entry can go stale mid-tick. When ON, the arm
    inline re-scores the symbol via run_momentum_neural_tick and confirms ONLY on the fresh
    score (never blind-touch freshness_ts).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.core import User
from app.models.trading import MomentumSymbolViability, TradingAutomationSession
from app.services.trading.momentum_neural import operator_actions

pytestmark = pytest.mark.usefixtures("stable_non_alpaca_account_identity")
from app.services.trading.momentum_neural.live_fsm import STATE_WATCHING_LIVE
from app.services.trading.momentum_neural.paper_fsm import STATE_LIVE_ARM_PENDING
from app.services.trading.momentum_neural.persistence import create_trading_automation_session
from app.services.trading_scheduler import _active_equity_session_symbols

from tests.test_momentum_paper_runner import _seed_live_eligible_row


def _uid(db: Session, name: str) -> int:
    u = User(name=f"VFP_{name}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return int(u.id)


# --------------------------------------------------------------------------- #
# (a) SCHEDULER — active-session symbols are surfaced for re-scoring           #
# --------------------------------------------------------------------------- #
def test_active_equity_session_symbols_surfaces_watched_name(db: Session) -> None:
    vid, _ = _seed_live_eligible_row(db, symbol="DXST")
    uid = _uid(db, "dxst")
    # an ACTIVE (watching) LIVE equity session — the consolidating watched name.
    create_trading_automation_session(
        db, user_id=uid, symbol="DXST", variant_id=vid, mode="live",
        state=STATE_WATCHING_LIVE, risk_snapshot_json={},
    )
    db.commit()
    syms = _active_equity_session_symbols(db)
    assert "DXST" in syms, "an active watched equity session must be surfaced for re-scoring"


def test_active_equity_session_symbols_excludes_crypto_and_inactive(db: Session) -> None:
    vid_c, _ = _seed_live_eligible_row(db, symbol="ETH-USD")
    uid = _uid(db, "mix")
    # crypto (excluded — has its own feed) + an inactive (finished) equity session.
    create_trading_automation_session(
        db, user_id=uid, symbol="ETH-USD", variant_id=vid_c, mode="live",
        state=STATE_WATCHING_LIVE, risk_snapshot_json={},
    )
    db.commit()
    syms = _active_equity_session_symbols(db)
    assert "ETH-USD" not in syms, "crypto is excluded (its own venue feed)"


def test_active_equity_session_symbols_empty_when_no_sessions(db: Session) -> None:
    assert _active_equity_session_symbols(db) == []


# --------------------------------------------------------------------------- #
# (b) CONFIRM — a stale-at-confirm row triggers the inline re-score            #
# --------------------------------------------------------------------------- #
def _make_arm_pending_session(db: Session, *, symbol: str, vid: int, uid: int, tok: str):
    return create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_ARM_PENDING,
        risk_snapshot_json={"arm_token": tok},
    )


def _set_row_age(db: Session, *, symbol: str, vid: int, age_seconds: float) -> None:
    row = (
        db.query(MomentumSymbolViability)
        .filter(MomentumSymbolViability.symbol == symbol, MomentumSymbolViability.variant_id == vid)
        .one()
    )
    row.freshness_ts = datetime.utcnow() - timedelta(seconds=age_seconds)
    db.commit()


def test_confirm_stale_row_triggers_inline_rescore(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_arm_time_viability_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0, raising=False)
    vid, _ = _seed_live_eligible_row(db, symbol="STALE")
    uid = _uid(db, "stale")
    _make_arm_pending_session(db, symbol="STALE", vid=vid, uid=uid, tok="tok-stale")
    _set_row_age(db, symbol="STALE", vid=vid, age_seconds=540.0)  # > 0.5 * 600 = 300s
    db.commit()

    called = {"n": 0, "tickers": None}

    def _spy_tick(_db, *, meta=None, **k):
        called["n"] += 1
        called["tickers"] = (meta or {}).get("tickers")
        return {"ok": True}

    # Re-score is the FIRST thing after the eligibility read; a later gate may block, but the
    # spy proves the inline refresh fired for the stale row.
    with patch("app.services.trading.momentum_neural.pipeline.run_momentum_neural_tick", _spy_tick):
        operator_actions.confirm_live_arm(db, user_id=uid, arm_token="tok-stale", confirm=True)
    assert called["n"] == 1, "a stale-at-confirm row must inline re-score the symbol"
    assert called["tickers"] == ["STALE"]


def test_confirm_fresh_row_does_not_rescore(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_arm_time_viability_refresh_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0, raising=False)
    vid, _ = _seed_live_eligible_row(db, symbol="FRESH")
    uid = _uid(db, "fresh")
    _make_arm_pending_session(db, symbol="FRESH", vid=vid, uid=uid, tok="tok-fresh")
    _set_row_age(db, symbol="FRESH", vid=vid, age_seconds=60.0)  # well under 300s
    db.commit()

    called = {"n": 0}

    def _spy_tick(_db, *, meta=None, **k):
        called["n"] += 1
        return {"ok": True}

    with patch("app.services.trading.momentum_neural.pipeline.run_momentum_neural_tick", _spy_tick):
        operator_actions.confirm_live_arm(db, user_id=uid, arm_token="tok-fresh", confirm=True)
    assert called["n"] == 0, "a fresh row must NOT trigger the inline re-score"


def test_confirm_flag_off_never_rescores(db: Session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_arm_time_viability_refresh_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0, raising=False)
    vid, _ = _seed_live_eligible_row(db, symbol="OFFV")
    uid = _uid(db, "offv")
    _make_arm_pending_session(db, symbol="OFFV", vid=vid, uid=uid, tok="tok-off")
    _set_row_age(db, symbol="OFFV", vid=vid, age_seconds=540.0)  # stale, but flag OFF
    db.commit()

    called = {"n": 0}

    def _spy_tick(_db, *, meta=None, **k):
        called["n"] += 1
        return {"ok": True}

    with patch("app.services.trading.momentum_neural.pipeline.run_momentum_neural_tick", _spy_tick):
        operator_actions.confirm_live_arm(db, user_id=uid, arm_token="tok-off", confirm=True)
    assert called["n"] == 0, "flag OFF -> confirm uses the row as-is (byte-identical)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
