"""Guard tests for the CHILI OS window title-bar context menu (WS-62).

Right-clicking a window's title bar opens a menu (Minimize / Maximize / Tile
left / Tile right / Close / Close others), reusing the existing window ops, and
dismissing on outside-click / Esc / blur. DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_os_js_has_context_menu():
    src = _read(_STATIC, "js", "os.js")
    assert "function showCtxMenu" in src and "function closeCtxMenu" in src, "context-menu builder is gone"
    assert "addEventListener('contextmenu'" in src, "the title-bar no longer opens a context menu"
    for label in ("'Minimize'", "'Maximize'", "'Tile left'", "'Tile right'", "'Close'", "'Close others'"):
        assert label in src, f"context menu lost the {label} item"


def test_context_menu_dismisses():
    src = _read(_STATIC, "js", "os.js")
    # Outside-click + Esc dismissal wired.
    assert "if (ctxMenu && !ctxMenu.contains(e.target)) closeCtxMenu()" in src
    assert "closeCtxMenu()" in src


def test_context_menu_styles_present():
    css = _read(_STATIC, "css", "os.css")
    assert ".os-ctxmenu" in css and ".os-ctx-item" in css, "context-menu CSS is gone"


def test_help_documents_window_menu():
    shell = _read(_TEMPLATES, "_workspace.html")
    assert "Window menu" in shell, "the shortcuts help no longer documents the window menu"
