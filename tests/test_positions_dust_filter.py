"""The cockpit 'phantom positions' fix (2026-06-16).

get_combined_positions() surfaced ~30 unsellable Coinbase dust / delisted residue
holdings (equity=0) as $0 'phantom positions'. _drop_dust_positions hides sub-floor
dust + delisted residue from the VIEW while keeping (and valuing) real holdings.
Display-only — reconcile paths read coinbase_service.get_positions() directly.
"""
from __future__ import annotations

import app.services.broker_manager as bm


def _patch_prices(monkeypatch, price_map):
    monkeypatch.setattr(bm.coinbase_service, "get_all_spot_prices", lambda: price_map)


def _cb(ticker, qty):
    return {"ticker": ticker, "quantity": qty, "equity": 0, "current_price": 0,
            "broker_source": bm.BROKER_COINBASE}


def test_dust_dropped_real_kept_and_enriched(monkeypatch):
    pm = {f"X{i}-USD": 1.0 for i in range(60)}  # substantial product list
    pm.update({"FAI-USD": 0.00259, "AMP-USD": 0.000523})
    _patch_prices(monkeypatch, pm)
    out = bm._drop_dust_positions([_cb("FAI-USD", 302664.0), _cb("AMP-USD", 210.0)])
    tickers = {p["ticker"] for p in out}
    assert "FAI-USD" in tickers        # 302664 * 0.00259 ~= $784 real -> kept
    assert "AMP-USD" not in tickers     # 210 * 0.000523 ~= $0.11 dust -> dropped
    fai = next(p for p in out if p["ticker"] == "FAI-USD")
    assert fai["equity"] > 700                 # enriched with real USD value
    assert fai["current_price"] == 0.00259


def test_delisted_dropped_when_map_substantial(monkeypatch):
    pm = {f"X{i}-USD": 1.0 for i in range(60)}  # substantial, DEAD-USD absent
    _patch_prices(monkeypatch, pm)
    assert bm._drop_dust_positions([_cb("DEAD-USD", 100.0)]) == []


def test_rh_position_always_kept(monkeypatch):
    pm = {f"X{i}-USD": 1.0 for i in range(60)}
    _patch_prices(monkeypatch, pm)
    rh = {"ticker": "IYH", "quantity": 5, "equity": 500, "broker_source": bm.BROKER_ROBINHOOD}
    assert bm._drop_dust_positions([rh]) == [rh]


def test_fail_open_on_empty_price_map(monkeypatch):
    # a transient price-fetch failure must NEVER blank real positions
    _patch_prices(monkeypatch, {})
    out = bm._drop_dust_positions([_cb("FAI-USD", 302664.0), _cb("AMP-USD", 210.0)])
    assert len(out) == 2


def test_fail_open_partial_map_keeps_absent_ticker(monkeypatch):
    # a tiny (<=50) map is treated as a partial fetch -> absent ticker kept, not hidden
    _patch_prices(monkeypatch, {"BTC-USD": 60000.0})
    assert len(bm._drop_dust_positions([_cb("SOL-USD", 10.0)])) == 1


def test_exactly_floor_is_kept(monkeypatch):
    pm = {f"X{i}-USD": 1.0 for i in range(60)}
    pm["ONE-USD"] = 1.0
    _patch_prices(monkeypatch, pm)
    # value == floor ($1.00) is NOT below floor -> kept
    out = bm._drop_dust_positions([_cb("ONE-USD", 1.0)])
    assert len(out) == 1
