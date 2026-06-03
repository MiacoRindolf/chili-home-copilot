from __future__ import annotations

from types import SimpleNamespace

import pytest


def _pattern(**overrides):
    data = {
        "win_rate": None,
        "oos_win_rate": None,
        "confidence": 0.0,
        "evidence_count": 0,
        "backtest_count": 0,
        "lifecycle_stage": "candidate",
        "promotion_status": "",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_pattern_quality_score_normalizes_probability_scales():
    from app.services.trading.opportunity_scoring import pattern_quality_score

    score = pattern_quality_score(
        _pattern(win_rate=60.0, oos_win_rate=0.55, confidence=75.0),
    )

    assert score == pytest.approx(0.3545)


def test_pattern_quality_score_preserves_zero_evidence():
    from app.services.trading.opportunity_scoring import pattern_quality_score

    score = pattern_quality_score(
        _pattern(win_rate=0.0, oos_win_rate=0.0, confidence=0.0),
    )

    assert score == 0.0


def test_pattern_quality_score_rejects_boolean_probability_evidence():
    from app.services.trading.opportunity_scoring import pattern_quality_score

    score = pattern_quality_score(
        _pattern(win_rate=True, oos_win_rate=True, confidence=True),
    )

    assert score == pytest.approx(0.07)
