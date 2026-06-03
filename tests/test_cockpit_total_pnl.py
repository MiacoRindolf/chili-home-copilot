"""Guard tests for the CHILI OS cockpit combined Total P/L (WS-58).

Total P/L = realized (24h net) + unrealized (open-position marks), shown as a
live KPI tile, plus each position's % move beside its dollar P/L. DB-free guards
(the pure numeric ``total`` from ``_aggregate`` is exercised against real code).
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


def test_aggregate_returns_numeric_total():
    from app.services.cockpit_pnl import _aggregate
    t = lambda T, e, q, d: types.SimpleNamespace(ticker=T, entry_price=e, quantity=q, direction=d)
    res = _aggregate(
        [t("AAPL", 100, 10, "long"), t("TSLA", 200, 5, "short"), t("MSFT", 300, 2, "long")],
        lambda x: {"AAPL": 110, "TSLA": 190, "MSFT": 290}[x],
    )
    assert res["total"] == 130.0, "numeric total (for combining with realized) is wrong"


def test_desktop_live_combines_realized_and_unrealized():
    src = _read(_SERVICES, "desktop_live.py")
    assert '"net_pnl": t.get("net_pnl")' in src, "numeric realized net_pnl must be exposed for the combine"
    assert "total_pnl_fmt" in src and "total_pnl_up" in src, "combined Total P/L is not computed"
    # The combine must only sum the parts that are actually known.
    assert "isinstance(x, (int, float))" in src, "the combine must guard part types"


def test_frontend_total_pnl_kpi_and_position_pct():
    js = _read(_STATIC, "js", "desktop.js")
    assert 'data-kpi="total_pnl"' in js and "total_pnl_fmt" in js, "Total P/L KPI wiring is gone"
    assert "p.pnl_pct_fmt" in js, "per-position % is no longer rendered"
    dash = _read(_TEMPLATES, "dashboard.html")
    assert 'data-kpi="total_pnl"' in dash, "the Total P/L KPI tile is missing from the dashboard"
    css = _read(_STATIC, "css", "workspace.css")
    assert ".ws-stat .pct" in css, "the muted %-move style is gone"
