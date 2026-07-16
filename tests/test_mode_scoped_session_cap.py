"""Mode-scoped concurrent-session cap (2026-06-12 SpaceX-morning incident).

10 overnight crypto PAPER sessions filled the mode-blind total cap and starved
every LIVE arm through the premarket window. Paper sessions are free
simulations — they must never consume the real-money concurrency budget.
"""

from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability, TradingAutomationSession
from app.services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation


def _seed(db, n_paper):
    from datetime import datetime, timezone

    from app.models.core import User

    u = User(name="cap-op")
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(family="mc", variant_key="mc_v", label="mc", params_json={})
    db.add(v)
    db.flush()
    db.add(MomentumSymbolViability(
        symbol="AZI", variant_id=int(v.id), scope="symbol", viability_score=0.8,
        live_eligible=True, freshness_ts=datetime.now(timezone.utc),
    ))
    for i in range(n_paper):
        db.add(TradingAutomationSession(
            user_id=int(u.id), symbol=f"PAP{i}-USD", mode="paper", state="watching",
            execution_family="coinbase_spot", variant_id=int(v.id), risk_snapshot_json={},
        ))
    db.flush()
    return int(u.id), int(v.id)


def test_paper_mass_does_not_starve_live_arms(db):
    uid, vid = _seed(db, n_paper=10)  # the exact incident shape
    ev = evaluate_proposed_momentum_automation(
        db, user_id=uid, symbol="AZI", variant_id=vid, mode="live",
        execution_family="robinhood_spot",
    )
    cap_checks = [c for c in ev["checks"] if c["id"] == "max_concurrent_sessions"]
    assert cap_checks and cap_checks[0]["ok"] is True, cap_checks


def test_paper_cap_still_binds_paper_proposals(db):
    uid, vid = _seed(db, n_paper=10)
    ev = evaluate_proposed_momentum_automation(
        db, user_id=uid, symbol="AZI", variant_id=vid, mode="paper",
        execution_family="coinbase_spot",
    )
    cap_checks = [c for c in ev["checks"] if c["id"] == "max_concurrent_sessions"]
    assert cap_checks and cap_checks[0]["ok"] is False


def test_alpaca_twins_do_not_consume_slots(db):
    """Every real arm spawns an alpaca twin — counting twins halves real
    capacity (2026-06-12 IPO morning). Twins are fake money; excluded."""
    from app.services.trading.momentum_neural.risk_evaluator import (
        count_concurrent_automation_sessions,
    )

    uid, vid = _seed(db, n_paper=0)
    for i in range(3):
        db.add(TradingAutomationSession(
            user_id=uid, symbol=f"EQ{i}", mode="live", state="queued_live",
            execution_family="robinhood_spot", variant_id=vid, risk_snapshot_json={},
        ))
        db.add(TradingAutomationSession(
            user_id=uid, symbol=f"EQ{i}", mode="live", state="queued_live",
            execution_family="alpaca_spot", variant_id=vid, risk_snapshot_json={},
        ))
        db.add(TradingAutomationSession(
            user_id=uid, symbol=f"SH{i}", mode="live", state="queued_live",
            execution_family="alpaca_short", variant_id=vid, risk_snapshot_json={},
        ))
    db.flush()
    assert count_concurrent_automation_sessions(db, user_id=uid, mode="live") == 3
