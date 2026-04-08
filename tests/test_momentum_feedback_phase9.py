"""Phase 9: closed-loop momentum automation feedback (neural evolution path)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.core import User
from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.evolution import paper_vs_live_performance_slices
from app.services.trading.momentum_neural.feedback_emit import try_emit_momentum_session_feedback
from app.services.trading.momentum_neural.outcome_extract import derive_outcome_class, session_terminal_for_feedback
from app.services.trading.momentum_neural.outcome_labels import (
    OUTCOME_CANCELLED_PRE_ENTRY,
    OUTCOME_NO_FILL,
    OUTCOME_SMALL_WIN,
    OUTCOME_SUCCESS,
)
from app.services.trading.momentum_neural.persistence import ensure_momentum_strategy_variants
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY


def test_derive_outcome_labels() -> None:
    ev: list = []
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="finished",
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=10.0,
            return_bps=30.0,
            exit_reason="target",
            governance_context={},
            events=ev,
        )
        == OUTCOME_SUCCESS
    )
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="finished",
            entry_occurred=True,
            partial_exit=False,
            realized_pnl_usd=1.0,
            return_bps=8.0,
            exit_reason="target",
            governance_context={},
            events=ev,
        )
        == OUTCOME_SMALL_WIN
    )
    assert (
        derive_outcome_class(
            mode="paper",
            terminal_state="cancelled",
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=ev,
        )
        == OUTCOME_CANCELLED_PRE_ENTRY
    )


def test_session_terminal_for_feedback_modes() -> None:
    assert session_terminal_for_feedback("paper", "finished")
    assert session_terminal_for_feedback("live", "live_finished")
    assert not session_terminal_for_feedback("paper", "watching")
    assert not session_terminal_for_feedback("live", "live_exited")


def test_zero_fill_maps_to_no_fill() -> None:
    class E:
        event_type = "live_error"
        payload_json = {"reason": "zero_fill"}

    assert (
        derive_outcome_class(
            mode="live",
            terminal_state="live_error",
            entry_occurred=False,
            partial_exit=False,
            realized_pnl_usd=None,
            return_bps=None,
            exit_reason=None,
            governance_context={},
            events=[E()],
        )
        == OUTCOME_NO_FILL
    )


def test_feedback_emit_idempotent(db: Session) -> None:
    """Requires migration 091 applied (test schema bootstrap)."""
    from sqlalchemy import inspect as sa_inspect

    names = set(sa_inspect(db.bind).get_table_names())
    if "momentum_automation_outcomes" not in names:
        pytest.skip("momentum_automation_outcomes table not present (run migrations)")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()

    u = User(name="FbPhase9")
    db.add(u)
    db.commit()
    db.refresh(u)

    sess = TradingAutomationSession(
        user_id=u.id,
        mode="paper",
        symbol="FB9-USD",
        variant_id=v.id,
        state="finished",
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_paper_execution": {"realized_pnl_usd": 5.0, "last_exit_reason": "target"},
        },
        ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)

    r1 = try_emit_momentum_session_feedback(db, sess)
    assert r1.get("emitted") or r1.get("ok")
    db.commit()

    r2 = try_emit_momentum_session_feedback(db, sess)
    assert r2.get("deduped")

    n = db.query(MomentumAutomationOutcome).filter(MomentumAutomationOutcome.session_id == sess.id).count()
    assert n == 1


def test_paper_vs_live_slices_separate(db: Session) -> None:
    from sqlalchemy import inspect as sa_inspect

    if "momentum_automation_outcomes" not in set(sa_inspect(db.bind).get_table_names()):
        pytest.skip("momentum_automation_outcomes table not present")

    ensure_momentum_strategy_variants(db)
    db.commit()
    v = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.family == "impulse_breakout").one()
    u = User(name="FbSlice")
    db.add(u)
    db.commit()
    db.refresh(u)

    for mode, rb in (("paper", 10.0), ("live", -15.0)):
        s = TradingAutomationSession(
            user_id=u.id,
            mode=mode,
            symbol="SL-USD",
            variant_id=v.id,
            state="finished" if mode == "paper" else "live_finished",
            risk_snapshot_json={RISK_SNAPSHOT_KEY: {"allowed": True}},
            ended_at=datetime.utcnow(),
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        try_emit_momentum_session_feedback(db, s)
        db.commit()

    pv = paper_vs_live_performance_slices(db, variant_id=v.id, days=14)
    assert pv["paper"]["n"] >= 1
    assert pv["live"]["n"] >= 1
    assert pv["paper"]["mean_return_bps"] is not None or pv["live"]["mean_return_bps"] is not None
