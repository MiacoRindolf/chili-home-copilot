"""g2 missing-stop coverage defers to the momentum lane for its live positions.

The momentum lane runs tight DYNAMIC stops on its own live positions. A general
g2 resting missing-stop on the same symbol would hold the base balance (blocking
the momentum market exit) and double-manage with a far/structure stop. So g2 must
skip a symbol while an active momentum live session owns it.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.trading.bracket_writer_g2 import _symbol_has_active_momentum_live_session

from tests.test_momentum_live_runner import _uid


def _seed_live_session(db: Session, *, suffix: str, symbol: str, state: str) -> None:
    from app.models.trading import MomentumStrategyVariant
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
        ensure_momentum_strategy_variants,
    )

    uid = _uid(db, suffix)
    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).first()
    sess = create_trading_automation_session(
        db,
        user_id=uid,
        symbol=symbol,
        variant_id=v.id,
        mode="live",
        state=state,
        risk_snapshot_json={"momentum_risk": {"allowed": True}},
        correlation_id=f"g2-{suffix}",
    )
    # Creation may normalize the initial state; force the exact state under test.
    if sess is not None and getattr(sess, "state", None) != state:
        sess.state = state
        db.add(sess)
    db.commit()


def test_g2_defers_to_active_momentum_live_session(db: Session) -> None:
    _seed_live_session(db, suffix="g2skip", symbol="TST-USD", state="live_entered")
    # exact + base-symbol match -> defer
    assert _symbol_has_active_momentum_live_session(db, "TST-USD") is True
    assert _symbol_has_active_momentum_live_session(db, "tst-usd") is True
    assert _symbol_has_active_momentum_live_session(db, "TST") is True
    # unrelated symbol -> no skip
    assert _symbol_has_active_momentum_live_session(db, "OTHER-USD") is False


def test_g2_no_skip_when_session_terminal(db: Session) -> None:
    _seed_live_session(db, suffix="g2term", symbol="TRM-USD", state="live_exited")
    # terminal session does not own the position -> g2 proceeds normally
    assert _symbol_has_active_momentum_live_session(db, "TRM-USD") is False


def test_g2_no_skip_when_no_session(db: Session) -> None:
    assert _symbol_has_active_momentum_live_session(db, "NONE-USD") is False
    assert _symbol_has_active_momentum_live_session(db, "") is False
