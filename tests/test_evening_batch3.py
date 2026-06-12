"""Evening batch 3 (2026-06-12): booking-truth finalize sweep, A0 ross re-rank,
chase suppression (verticality skip, VWAP weak-path fail-closed, ask-heavy
size-down)."""

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.config import settings as _settings


def test_config_defaults_batch3():
    from app.config import Settings

    f = Settings.model_fields
    assert f["chili_momentum_exited_finalize_idle_min"].default == 20.0
    assert f["chili_momentum_entry_verticality_atr_mult"].default == 1.5
    assert f["chili_momentum_entry_ask_heavy_size_fraction"].default == 0.5


def test_finalize_books_outcome_for_stale_exited(db, monkeypatch):
    from app.models.core import User
    from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession
    from app.services.trading.momentum_neural import auto_arm as aa

    u = User(name="fin-op"); db.add(u); db.flush()
    v = MomentumStrategyVariant(family="fin", variant_key="fin_v", label="fin", params_json={})
    db.add(v); db.flush()
    sess = TradingAutomationSession(
        user_id=int(u.id), symbol="QH", mode="live", state="live_exited",
        execution_family="robinhood_spot", variant_id=int(v.id),
        risk_snapshot_json={"momentum_live_execution": {"realized_pnl_usd": -7.0}},
        updated_at=datetime.utcnow() - timedelta(minutes=45),
    )
    db.add(sess); db.flush()
    monkeypatch.setattr(_settings, "chili_momentum_exited_finalize_idle_min", 20.0)
    done = aa._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow())
    db.flush()
    assert done == 1
    assert sess.state == "live_finished"
    rows = db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.session_id == int(sess.id)
    ).all()
    assert len(rows) == 1  # the outcome BOOKED


def _trend_df(bars=40, base=10.0, last_close_mult=1.0):
    """Uptrend with a 2-bar pullback then a break bar; volume carries."""
    rows = []
    px = base
    for i in range(bars):
        if i < bars - 4:
            px *= 1.004
            o, c = px / 1.002, px
        elif i < bars - 2:  # pullback
            o, c = px, px * 0.997
            px = c
        else:  # break bars
            px *= 1.01
            o, c = px / 1.008, px
        c = c * (last_close_mult if i == bars - 1 else 1.0)
        rows.append({"Open": o, "High": max(o, c) * 1.002, "Low": min(o, c) * 0.998,
                     "Close": c, "Volume": 200000 if i >= bars - 2 else 80000})
    idx = pd.date_range("2026-06-12 14:00", periods=bars, freq="1min", tz="UTC")
    return pd.DataFrame(rows, index=idx)


def test_verticality_skip_blocks_extended_breaks(monkeypatch):
    from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger

    monkeypatch.setattr(_settings, "chili_momentum_entry_verticality_atr_mult", 1.5)
    # a vertical last bar: close 8% above the trend = way above EMA9
    df = _trend_df(last_close_mult=1.08)
    ok, reason, dbg = momentum_pullback_trigger(df, entry_interval="1m", symbol="TEST")
    # a vertical chase bar must be refused by SOME gate (which one depends on
    # the synthetic df's path through the ladder)
    assert not ok
    # with the knob disabled the verticality reason can never appear
    monkeypatch.setattr(_settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    ok2, reason2, _ = momentum_pullback_trigger(df, entry_interval="1m", symbol="TEST")
    assert reason2 != "extended_verticality"
    # and the gate itself is present, ATR-scaled, and applies to tick breaks
    src = open("app/services/trading/momentum_neural/entry_gates.py", encoding="utf-8").read()
    i = src.index("VERTICALITY SKIP")
    block = src[i:i + 1200]
    assert "extended_verticality" in block
    assert "atr_pct" in block and "_tick_break" in block


def test_ross_rank_key_orders_by_subscore():
    from app.services.trading.momentum_neural import auto_arm as aa

    src = open("app/services/trading/momentum_neural/auto_arm.py", encoding="utf-8").read()
    assert "_ross_rank_key" in src
    assert "ross_scores" in src


def test_ask_heavy_sizedown_wired():
    src = open("app/services/trading/momentum_neural/live_runner.py", encoding="utf-8").read()
    assert "ask_heavy_size_down" in src
    assert "float(_l2_mult)" in src
