"""Tick-level TRADE-FLOW (aggressor signed-volume imbalance) — the equity trade-tape feature
(2026-06-23). Proves the aggressor classification (quote-rule + tick-rule fallback), the crypto
microstructure path, and the capture wiring. The IQFeed bridge's DB path is smoke-tested separately
via `iqfeed_trade_bridge.py --selftest`."""
from __future__ import annotations

from app.services.trading.momentum_neural import meta_label as ml
from app.services.trading.momentum_neural.pipeline import _aggressor_imbalance, _live_trade_flow


def test_aggressor_all_buys_quote_rule():
    rows = [(10.0, 100, 9.9, 10.0), (10.1, 200, 10.0, 10.1), (10.2, 100, 10.1, 10.2)]  # px>=ask
    assert abs(_aggressor_imbalance(rows) - 1.0) < 1e-9


def test_aggressor_all_sells_quote_rule():
    rows = [(9.9, 100, 9.9, 10.0), (9.8, 200, 9.8, 9.9)]                                # px<=bid
    assert abs(_aggressor_imbalance(rows) + 1.0) < 1e-9


def test_aggressor_volume_weighted():
    rows = [(10.0, 100, 9.9, 10.0), (9.9, 300, 9.9, 10.0)]            # +100 buy, -300 sell over 400
    assert abs(_aggressor_imbalance(rows) - (-0.5)) < 1e-9


def test_aggressor_tick_rule_no_quote():
    # no bid/ask -> tick rule. first(prev=None)->0; up->+1; down->-1 ; (0+100-100)/300 = 0.0
    rows = [(10.0, 100, None, None), (10.1, 100, None, None), (10.0, 100, None, None)]
    assert abs(_aggressor_imbalance(rows) - 0.0) < 1e-9


def test_aggressor_empty_and_zero_size():
    assert _aggressor_imbalance([]) is None
    assert _aggressor_imbalance([(0.0, 0.0, None, None)]) is None     # zero volume -> None


def test_live_trade_flow_crypto_path(monkeypatch):
    import app.services.trading.microstructure as msx

    class _F:
        trade_aggression = 0.8

    monkeypatch.setattr(msx, "get_features", lambda pid: _F())
    assert abs(_live_trade_flow("BTC-USD", db=None, as_of=None) - 0.6) < 1e-9   # 2*0.8-1


def test_live_trade_flow_crypto_historical_asof_none():
    # crypto has no historical tape -> as_of (replay) returns None (no false data)
    assert _live_trade_flow("BTC-USD", db=None, as_of="2026-06-22 14:00:00") is None


def test_trade_flow_in_default_features():
    assert "trade_flow" in ml.DEFAULT_FEATURES


def test_capture_includes_trade_flow(monkeypatch):
    import app.services.trading.momentum_neural.pipeline as pl
    from app.services.trading.momentum_neural.entry_features import capture_entry_features

    monkeypatch.setattr(pl, "_live_trade_flow", lambda symbol, db=None, as_of=None: 0.42)
    f = capture_entry_features("AAA", fill_px=1.6, stop=1.55, target=1.7, qty=100, want_qty=120,
                               spread_bps=20, atr_pct=0.02, stop_atr_pct_eff=0.03, mid=1.6,
                               dollar_vol=1e6, liq_mult=1.0, fire_ts="2026-06-22 14:05",
                               entry_fidelity="live", session_df=None, l2_db=None, l2_as_of=None)
    assert f.get("trade_flow") == 0.42
