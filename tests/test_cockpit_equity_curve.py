"""Guard tests for the CHILI OS cockpit intraday realized-P/L sparkline (WS-59).

A cumulative realized-P/L curve over the trailing 24h (closed trades only — no
quotes), drawn as an inline SVG polyline. The pure ``_cumulative`` helper is
tested against real code; the rest are DB-free wiring guards, including a guard
that the new sparkline uses its own ``ws-eqspark`` class (not the pre-existing,
absolutely-positioned ``.ws-spark``).
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")
_SERVICES = os.path.join(_HERE, "..", "app", "services")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_cumulative_running_sum_and_cap():
    from app.services.cockpit_pnl import _cumulative
    r = _cumulative([10, -5, 20, -3])
    assert r["points"] == [10.0, 5.0, 25.0, 22.0]
    assert r["count"] == 4 and r["last_fmt"] == "$22.00" and r["up"] is True
    assert _cumulative([])["points"] == [] and _cumulative([])["last_fmt"] is None
    assert len(_cumulative([1.0] * 100)["points"]) == 60   # capped to most-recent 60


def test_curve_query_is_read_only():
    # The curve query must select, never write.
    src = _read(_SERVICES, "cockpit_pnl.py")
    assert "build_intraday_curve" in src
    for forbidden in (".commit(", ".add(", ".delete(", "INSERT", "UPDATE "):
        assert forbidden not in src, f"cockpit_pnl must stay read-only — found {forbidden!r}"


def test_desktop_live_includes_equity_curve():
    src = _read(_SERVICES, "desktop_live.py")
    assert "equity_curve" in src and "build_intraday_curve" in src


def test_frontend_sparkline_wiring_and_no_class_collision():
    js = _read(_STATIC, "js", "desktop.js")
    assert "function renderSpark" in js and "equity_curve" in js, "sparkline render wiring is gone"
    assert "polyline" in js, "the SVG polyline is no longer drawn"
    dash = _read(_TEMPLATES, "dashboard.html")
    assert 'id="ws-equity-spark"' in dash and 'class="ws-eqspark"' in dash, "sparkline element/class missing"
    css = _read(_STATIC, "css", "workspace.css")
    assert ".ws-eqspark{" in css, "the ws-eqspark style is gone"
    # The pre-existing absolutely-positioned .ws-spark must NOT be what the new
    # element relies on (collision would push it off-screen).
    assert 'class="ws-spark"' not in dash, "sparkline must use ws-eqspark, not the colliding ws-spark"
