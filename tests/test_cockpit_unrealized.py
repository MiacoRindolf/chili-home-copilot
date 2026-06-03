"""Tests for the CHILI OS cockpit per-position unrealized P/L.

``cockpit_pnl.build_unrealized`` is a READ-ONLY enrichment: it reads open Trade
rows + live quotes and computes per-position unrealized P/L (reusing the desk's
``_compute_unrealized``). The core ``_aggregate`` is pure (quote function injected)
so it's tested deterministically with no DB / network. The rest are DB-free
source guards on the wiring + a guard that the service never writes.
"""
import os
import types

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")
_SERVICES = os.path.join(_HERE, "..", "app", "services")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def _trade(ticker, entry, qty, direction):
    o = types.SimpleNamespace()
    o.ticker, o.entry_price, o.quantity, o.direction = ticker, entry, qty, direction
    return o


def test_aggregate_computes_long_short_and_total():
    from app.services.cockpit_pnl import _aggregate
    trades = [
        _trade("AAPL", 100, 10, "long"),    # (110-100)*10 = +100
        _trade("TSLA", 200, 5, "short"),    # (200-190)*5  = +50
        _trade("NVDA", 50, 4, "long"),      # no quote -> unpriced
        _trade("MSFT", 300, 2, "long"),     # (290-300)*2  = -20
    ]
    prices = {"AAPL": 110, "TSLA": 190, "MSFT": 290}
    res = _aggregate(trades, lambda t: prices.get(t))

    assert res["count"] == 4 and res["priced"] == 3
    assert res["total_fmt"] == "$130.00" and res["total_up"] is True
    assert res["by_ticker"]["AAPL"] == {"priced": True, "pnl_fmt": "$100.00", "pnl_up": True, "pnl_pct_fmt": "+10.00%"}
    assert res["by_ticker"]["TSLA"]["pnl_fmt"] == "$50.00"
    assert res["by_ticker"]["NVDA"] == {"priced": False}          # no quote → unpriced, not counted
    assert res["by_ticker"]["MSFT"]["pnl_fmt"] == "-$20.00" and res["by_ticker"]["MSFT"]["pnl_up"] is False


def test_aggregate_empty_and_quote_failure_are_safe():
    from app.services.cockpit_pnl import _aggregate
    assert _aggregate([], lambda t: 1.0)["total_fmt"] is None
    # A quote_fn that throws must not break the aggregate (position stays unpriced).

    def boom(_):
        raise RuntimeError("quote provider down")

    res = _aggregate([_trade("AAPL", 100, 1, "long")], boom)
    assert res["priced"] == 0 and res["by_ticker"]["AAPL"] == {"priced": False}


def test_service_is_read_only():
    # The cockpit P/L service must never write — no commits, no order placement.
    src = _read(_SERVICES, "cockpit_pnl.py")
    for forbidden in (".commit(", ".add(", ".delete(", "place_order", "submit_order", "INSERT", "UPDATE "):
        assert forbidden not in src, f"cockpit_pnl must be read-only — found {forbidden!r}"


def test_desktop_live_merges_unrealized():
    src = _read(_SERVICES, "desktop_live.py")
    assert "_merge_unrealized" in src and "build_unrealized" in src
    assert "unrealized_total_fmt" in src


def test_frontend_renders_position_pnl_and_total():
    js = _read(_STATIC, "js", "desktop.js")
    assert "p.pnl_fmt" in js, "renderPositions no longer shows per-position P/L"
    assert "ws-unrealized" in js and "unrealized_total_fmt" in js, "the total-unrealized chip wiring is gone"
    dash = _read(_TEMPLATES, "dashboard.html")
    assert 'id="ws-unrealized"' in dash, "the positions card lost its total-unrealized chip"
