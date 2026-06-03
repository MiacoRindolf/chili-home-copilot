"""Guard tests for CHILI OS bulk window commands.

⌘K commands for managing many windows at once: minimize all, restore all
minimized, and close all (close-all routes through the per-window close path so
each window lands on the reopen stack — ⌘⌥T brings them back). DB-free source
guards: no Postgres, no fixtures.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_os_js_exposes_bulk_actions():
    src = _read(_STATIC, "js", "os.js")
    for api in ("closeAll:", "minimizeAll:", "restoreMinimized:"):
        assert api in src, f"ChiliOS.{api} bulk-action API is gone"
    # close-all must reuse closeApp so each window is reopenable (not closeAllNow).
    close_all_region = src[src.index("closeAll:"):src.index("minimizeAll:")]
    assert "closeApp(a)" in close_all_region, "closeAll must route through closeApp (reopen-aware)"


def test_palette_has_bulk_commands():
    src = _read(_STATIC, "js", "workspace.js")
    for cmd in ("cmd: 'min-all'", "cmd: 'restore-all'", "cmd: 'close-all'"):
        assert cmd in src, f"⌘K {cmd} command is gone"
    for call in ("window.ChiliOS.minimizeAll()", "window.ChiliOS.restoreMinimized()", "window.ChiliOS.closeAll()"):
        assert call in src, f"the command no longer drives {call}"
