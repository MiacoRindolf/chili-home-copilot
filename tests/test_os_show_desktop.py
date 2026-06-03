"""Guard tests for the CHILI OS "show desktop" toggle (WS-64).

The dock's Dashboard button now toggles: first click hides all visible windows
to reveal the desktop home; click again restores them where they were. The home
also brightens when nothing is on top of it (appsOpen counts only visible
windows). DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_dashboard_button_is_a_toggle():
    src = _read(_STATIC, "js", "os.js")
    assert "deskHidden" in src, "the show-desktop toggle state is gone"
    assert "deskHidden.forEach" in src, "the restore branch (re-show hidden windows) is gone"
    assert "show desktop" in src.lower(), "the show-desktop intent comment/label is gone"


def test_appsopen_counts_only_visible_windows():
    src = _read(_STATIC, "js", "os.js")
    i = src.index("function appsOpen()")
    body = src[i:i + 200]
    assert "display !== 'none'" in body, "appsOpen must count only visible windows (so the home brightens)"
