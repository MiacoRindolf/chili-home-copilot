"""Parity: unified ``Signal`` reflects the same levels/side as bespoke pick + proposal.

Phase 1 does not change auto-trader code; this guards that the additive row is a faithful
encoding of the proposal path inputs (30-day replay harness is deferred).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.services.trading.contracts.signal_emit import build_signal_from_strategy_pick


def test_parity_signal_matches_proposal_levels() -> None:
    pick = {
        "ticker": "XAU",
        "is_crypto": False,
        "combined_score": 6.0,
        "signals": ["breakout"],
        "indicators": {"atr": 2.5},
        "scan_pattern_id": 99,
        "signal": "buy",
    }
    now = datetime.utcnow()
    exp = now + timedelta(hours=24)
    prop = SimpleNamespace(
        id=42,
        entry_price=100.0,
        stop_loss=94.0,
        take_profit=112.0,
        proposed_at=now,
        expires_at=exp,
        direction="long",
    )
    tr = {"type": "breakout", "label": "Breakout", "duration": ""}
    sig = build_signal_from_strategy_pick(
        pick=pick,
        proposal_id=42,
        entry=float(prop.entry_price),
        stop=float(prop.stop_loss),
        target=float(prop.take_profit),
        trade_class=tr,
        timeframe_label="Breakout",
        created_at=now,
        expires_at=exp,
        scanner="top_pick",
        strategy_family="fam",
        proposal_direction=prop.direction,
    )
    assert sig.symbol == "XAU"
    assert sig.entry_price == Decimal("100")
    assert sig.stop_price == Decimal("94")
    assert sig.take_profit_price == Decimal("112")
    assert sig.side == "long"
    assert "breakout" in sig.rule_fires
    assert sig.features.get("strategy_proposal_id") == 42
    assert sig.pattern_id == "99"
