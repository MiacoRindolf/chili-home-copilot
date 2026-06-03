"""Guard tests for the CHILI OS cockpit session-progress underline.

While the market is open, the session-countdown chip grows a 2px accent
underline that fills left→right as the regular session (9:30–16:00 ET) elapses.
DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_session_progress_helper_and_render():
    src = _read(_STATIC, "js", "desktop.js")
    assert "function sessionProgress" in src, "sessionProgress(open, t) helper is gone"
    # Only fills while open; clears the underline otherwise.
    assert "open !== true" in src, "sessionProgress must return null unless the market is open"
    assert "backgroundImage" in src, "renderSession no longer paints the progress underline"
    assert "sessionProgress: sessionProgress" in src, "ChiliDesktop.sessionProgress is not exposed"
