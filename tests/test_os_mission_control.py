"""Guard tests for CHILI OS "Mission Control" (window switcher / exposé).

Mission Control overlays every open window as a keyboard-navigable card grid,
triggered by ``⌘/Ctrl+Alt+E`` or the ⌘K palette. These are **DB-free** source
guards (no Postgres, no fixtures): they assert the feature's wiring stays intact
across os.js (window manager), workspace.js (palette command), os.css (overlay
styles) and the keyboard-shortcuts help. A future refactor that drops a piece
trips one of these instead of silently regressing the switcher.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_os_js_exposes_mission_control_api():
    src = _read(_STATIC, "js", "os.js")
    # The overlay builder + the public toggle on the ChiliOS API.
    assert "function openExpose" in src, "openExpose() builder is gone"
    assert "expose: openExpose" in src, "ChiliOS.expose API entry is gone"
    # Restores a minimized window when picked (reuses the taskbar-restore path).
    assert "function pickExpose" in src and "removeChip(app)" in src


def test_os_js_binds_the_keyboard_shortcut():
    src = _read(_STATIC, "js", "os.js")
    # ⌘/Ctrl+Alt+E opens it; Esc / arrows / Enter drive it in capture phase.
    assert "=== 'e'" in src and "openExpose()" in src, "⌘⌥E binding is gone"
    assert "function exposeSelect" in src, "keyboard navigation handler is gone"


def test_palette_has_mission_control_command():
    src = _read(_STATIC, "js", "workspace.js")
    assert "cmd: 'expose'" in src, "⌘K Mission Control command is gone"
    assert "window.ChiliOS.expose()" in src, "the command no longer drives ChiliOS.expose"


def test_overlay_styles_present():
    css = _read(_STATIC, "css", "os.css")
    assert ".os-expose" in css and ".os-xcard" in css, "Mission Control overlay CSS is gone"


def test_help_lists_mission_control_shortcut():
    shell = _read(_TEMPLATES, "_workspace.html")
    assert "Mission Control" in shell, "the shortcuts help no longer documents Mission Control"
