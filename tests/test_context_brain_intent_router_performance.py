from __future__ import annotations

from app.services.context_brain import intent_router
from app.services.context_brain.types import INTENT_CODE, INTENT_TRADING


def test_score_keywords_uses_precompiled_patterns(monkeypatch) -> None:
    calls = 0

    def fail_re_search(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("_score_keywords should use precompiled regexes")

    monkeypatch.setattr(intent_router.re, "search", fail_re_search)

    scores = intent_router._score_keywords("Fix the pytest failure in the Coinbase trading adapter")

    assert INTENT_CODE in scores
    assert INTENT_TRADING in scores
    assert calls == 0


def test_score_keywords_signals_keep_original_pattern_text() -> None:
    scores = intent_router._score_keywords("Please debug this Python traceback")

    _score, signals = scores[INTENT_CODE]

    assert r"\b(bug|fix|refactor|implement|debug|exception|error|stacktrace|traceback)\b" in {
        signal.removeprefix("kw:") for signal in signals
    }
