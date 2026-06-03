"""Guard tests for CHILI OS deep-link window-title context (WS-65).

A window opened on a ticker (e.g. ChiliOS.open('trading', '/trading?ticker=NVDA'))
shows "Trading Desk · NVDA". The context comes from the URL, so it must be
HTML-escaped when rendered. DB-free source guards.
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_title_context_helpers():
    src = _read(_STATIC, "js", "os.js")
    assert "function ctxLabel" in src and "function titleWithCtx" in src, "title-context helpers are gone"
    assert "get('ticker')" in src and "get('symbol')" in src, "ctxLabel no longer reads ticker/symbol"
    assert "dispTitle = titleWithCtx(cfg.title, cfg.src)" in src, "dispTitle is not computed in openApp"


def test_title_context_is_escaped():
    # The URL-derived title must be escaped in the new-window markup, and set via
    # textContent on re-point — never raw innerHTML.
    src = _read(_STATIC, "js", "os.js")
    assert "escHtml(dispTitle)" in src, "the window title must be HTML-escaped (XSS-safe)"
    assert "wt0.textContent = dispTitle" in src, "re-point must set the title via textContent (XSS-safe)"
    assert "el.dataset.title = dispTitle" in src, "dataset.title should carry the context (taskbar/restore)"
