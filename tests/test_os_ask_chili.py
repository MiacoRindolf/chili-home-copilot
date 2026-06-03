"""Guard tests for CHILI OS "Ask CHILI" (⌘K → Chat).

Typing free text in the ⌘K palette offers an "Ask CHILI" result that opens the
Chat window pre-loaded with the question (NOT auto-sent — the user reviews and
presses Enter). DB-free source guards across workspace.js (palette result) and
chat.html (the ?q= prefill).
"""
import os

_HERE = os.path.dirname(__file__)
_STATIC = os.path.join(_HERE, "..", "app", "static")
_TEMPLATES = os.path.join(_HERE, "..", "app", "templates")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def test_palette_offers_ask_chili():
    src = _read(_STATIC, "js", "workspace.js")
    assert "function askResults" in src, "askResults builder is gone"
    assert "Ask CHILI" in src, "the Ask CHILI label is gone"
    assert "'/chat?q=' + encodeURIComponent" in src, "Ask CHILI must deep-link to /chat?q=<encoded>"
    assert ".concat(askResults(q))" in src, "askResults is not wired into the palette results"


def test_chat_prefills_from_q_without_autosend():
    src = _read(_TEMPLATES, "chat.html")
    assert "URLSearchParams(window.location.search).get('q')" in src, "chat no longer reads ?q="
    assert "input.value = _askQ" in src, "chat no longer prefills the composer from ?q="
    # The prefill block must NOT auto-submit — only prefill + focus.
    i = src.index("var _askQ")
    block = src[i:i + 500]
    assert "submit" not in block.lower(), "Ask CHILI prefill must not auto-send the message"
