"""Paper runner quote sanity (2026-06-12 ROBO-USD incident).

A failed quote fetch used to fabricate a $100.0 placeholder mid; on a $0.022
token that "filled" a partial exit at $99.84 and minted +$555,963 of fictional
realized PnL. These tests pin the two guards:
 1. _resolve_quote NEVER invents a price level (no-quote -> zeros).
 2. tick_paper_session skips the tick on no-quote and quarantines a one-tick
    mid jump beyond the guard fraction.
"""

from app.services.trading.momentum_neural import paper_runner as pr
from app.services.trading.momentum_neural.paper_runner import (
    _resolve_quote,
    tick_paper_session,
)


def test_resolve_quote_never_fabricates_price():
    bid, ask, mid, src = _resolve_quote("ROBO-USD", 25.0, lambda s: {"mid": None})
    assert (bid, ask, mid) == (0.0, 0.0, 0.0)
    assert src == "quote_unavailable"


def test_resolve_quote_synthesizes_spread_around_real_mid_only():
    bid, ask, mid, src = _resolve_quote("ROBO-USD", 25.0, lambda s: {"mid": 0.0224})
    assert abs(mid - 0.0224) < 1e-9
    assert 0 < bid < mid < ask
    assert src == "synthetic_spread"


def _mk_session(db, *, last_mid=None):
    from datetime import datetime, timezone

    from app.models.trading import (
        MomentumStrategyVariant,
        MomentumSymbolViability,
        TradingAutomationSession,
    )
    from app.services.trading.momentum_neural.paper_runner import KEY_PAPER_EXEC
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY

    v = MomentumStrategyVariant(family="qs", variant_key="qs_v", label="qs", params_json={})
    db.add(v)
    db.flush()
    db.add(MomentumSymbolViability(
        symbol="ROBO-USD", variant_id=int(v.id), scope="symbol", viability_score=0.8,
        live_eligible=True, freshness_ts=datetime.now(timezone.utc),
    ))
    pe = {"tick_count": 1}
    if last_mid is not None:
        pe["last_mid"] = last_mid
    sess = TradingAutomationSession(
        symbol="ROBO-USD",
        variant_id=int(v.id),
        mode="paper",
        state="watching",
        execution_family="coinbase_spot",
        risk_snapshot_json={RISK_SNAPSHOT_KEY: {}, KEY_PAPER_EXEC: pe},
    )
    db.add(sess)
    db.flush()
    return sess


def _enable_runner(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "chili_momentum_paper_runner_enabled", True)


def test_tick_skips_when_no_quote(db, monkeypatch):
    _enable_runner(monkeypatch)
    sess = _mk_session(db)
    out = tick_paper_session(db, int(sess.id), quote_fn=lambda s: {"mid": None})
    assert out.get("skipped") == "quote_unavailable"


def test_tick_quarantines_one_tick_price_jump(db, monkeypatch):
    _enable_runner(monkeypatch)
    sess = _mk_session(db, last_mid=0.0224)
    out = tick_paper_session(
        db, int(sess.id), quote_fn=lambda s: {"mid": 99.84, "bid": 99.8, "ask": 99.88}
    )
    assert out.get("skipped") == "quote_quarantined"


def test_tick_accepts_normal_move(db, monkeypatch):
    _enable_runner(monkeypatch)
    sess = _mk_session(db, last_mid=0.0224)
    out = tick_paper_session(
        db, int(sess.id), quote_fn=lambda s: {"mid": 0.0230, "bid": 0.0229, "ask": 0.0231}
    )
    assert out.get("skipped") != "quote_quarantined"
    assert out.get("skipped") != "quote_unavailable"
