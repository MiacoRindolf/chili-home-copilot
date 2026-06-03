"""Guard tests for the CHILI OS cockpit market-session countdown.

The desktop cockpit shows "Opens · 1h 30m" / "Closes · 2h 14m" next to the
market pill, ticking client-side off the ET clock. The open/closed *state* comes
from the backend poll; the countdown targets the US equities regular session
(9:30–16:00 ET, Mon–Fri) and is holiday-safe ("Market closed" if the backend
reports closed during regular hours). DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_desktop_js_has_session_countdown_logic():
    src = _read(_STATIC, "js", "desktop.js")
    for fn in ("function sessionLabel", "function etNow", "function minsToNextOpen", "function renderSession"):
        assert fn in src, f"{fn} is gone from the cockpit"
    # The poll must stash the open flag and the tick must re-render the countdown.
    assert "mktOpen =" in src, "the poll no longer stashes the market open flag"
    assert "renderSession()" in src, "renderSession is never called"
    # Holiday-safe branch + the regular-session bounds.
    assert "'Market closed'" in src, "the holiday/halt guard label is gone"
    assert "9 * 60 + 30" in src and "16 * 60" in src, "session bounds (9:30–16:00) changed unexpectedly"


def test_desktop_js_exposes_pure_session_label():
    src = _read(_STATIC, "js", "desktop.js")
    assert "window.ChiliDesktop" in src and "sessionLabel: sessionLabel" in src, \
        "ChiliDesktop.sessionLabel must be exposed (pure helper for widgets/tests)"


def test_cockpit_markup_and_style_present():
    dash = _read(_TEMPLATES, "dashboard.html")
    assert 'id="ws-mkt-countdown"' in dash, "the cockpit lost the session-countdown element"
    css = _read(_STATIC, "css", "workspace.css")
    assert ".ws-cockpit-session" in css, "the session-countdown chip style is gone"
