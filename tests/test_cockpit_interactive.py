"""Guard tests for the interactive CHILI OS cockpit (WS-63).

Clicking a cockpit KPI tile or safety pill opens the relevant app (P/L & counts →
Trading Desk; patterns → Brain) via the OS's existing ``[data-os-open]`` plumbing.
DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_cockpit_elements_are_clickable():
    dash = _read(_TEMPLATES, "dashboard.html")
    # Safety pills open the desk.
    assert 'id="ws-killswitch" data-os-open="trading"' in dash, "kill-switch pill is not clickable"
    assert 'id="ws-breaker" data-os-open="trading"' in dash, "breaker pill is not clickable"
    # KPI tiles map to apps (trading / brain) and the Total P/L tile opens the desk.
    assert "_kapp = {'net_pnl':'trading'" in dash and "'patterns':'brain'" in dash, "KPI→app map is gone"
    assert 'data-kpi="total_pnl" data-os-open="trading"' in dash, "Total P/L tile is not clickable"


def test_clickable_affordance_css():
    css = _read(_STATIC, "css", "workspace.css")
    assert ".ws-kpi[data-os-open]" in css and ".ws-cockpit-pill[data-os-open]" in css, "clickable affordance CSS is gone"
    assert "cursor:pointer" in css


def test_os_wires_data_os_open():
    # The plumbing the cockpit relies on: os.js opens the app for [data-os-open].
    src = _read(_STATIC, "js", "os.js")
    assert "[data-os-open]" in src and "el.dataset.osOpen" in src, "the data-os-open wiring is gone from os.js"
