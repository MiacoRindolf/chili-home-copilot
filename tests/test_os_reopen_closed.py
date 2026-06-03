"""Guard tests for CHILI OS "Reopen closed window".

Closing a window remembers it; ``⌘/Ctrl+Alt+T`` (or the ⌘K palette) reopens the
most-recently-closed window where it was. These are **DB-free** source guards
(no Postgres, no fixtures): they assert the closed-stack wiring stays intact
across os.js (window manager), workspace.js (palette command) and the help sheet.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_os_js_tracks_and_reopens_closed_windows():
    src = _read(_STATIC, "js", "os.js")
    # closeApp must record the window before tearing it down; reopen pops it back.
    assert "function pushClosed" in src and "function reopenClosed" in src
    assert "pushClosed({" in src, "closeApp no longer records the closed window"
    assert "reopenClosed: reopenClosed" in src, "ChiliOS.reopenClosed API entry is gone"


def test_reopen_keyboard_shortcut_works_with_zero_windows():
    src = _read(_STATIC, "js", "os.js")
    # The ⌘⌥T handler must sit BEFORE the `if (!el) return;` no-window guard,
    # otherwise reopening after closing the last window would be impossible.
    idx_t = src.find("reopenClosed(); return;")
    idx_guard = src.find("var top = order[order.length - 1], el = top && wins[top];")
    assert idx_t != -1 and idx_guard != -1, "expected the ⌘⌥T handler and the no-window guard"
    assert idx_t < idx_guard, "⌘⌥T reopen must be handled before the no-window guard"


def test_palette_has_reopen_command():
    src = _read(_STATIC, "js", "workspace.js")
    assert "cmd: 'reopen'" in src, "⌘K Reopen-closed command is gone"
    assert "window.ChiliOS.reopenClosed()" in src, "the command no longer drives ChiliOS.reopenClosed"


def test_help_lists_reopen_shortcut():
    shell = _read(_TEMPLATES, "_workspace.html")
    assert "Reopen closed window" in shell, "the shortcuts help no longer documents reopen-closed"
