from __future__ import annotations

from types import SimpleNamespace

import pytest


class _PatternQuery:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self) -> list[object]:
        return self._rows


class _PatternDb:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return _PatternQuery(self._rows)


def _pattern(**overrides):
    data = {
        "id": 1,
        "name": "Pattern",
        "lifecycle_stage": "promoted",
        "confidence": 0.7,
        "oos_win_rate": None,
        "win_rate": None,
        "avg_return_pct": 0.0,
        "timeframe": "1d",
        "asset_class": "all",
        "rules_json": "{}",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_trade_ideas_respect_zero_oos_win_rate_without_legacy_fallback():
    from app.services.trading.daily_playbook import _generate_trade_ideas

    pat = _pattern(
        id=7,
        name="Zero OOS evidence",
        oos_win_rate=0.0,
        win_rate=0.9,
        avg_return_pct=5.0,
    )

    ideas = _generate_trade_ideas(_PatternDb([pat]), user_id=None, capital=100_000)

    assert ideas[0]["pattern_id"] == 7
    assert ideas[0]["oos_win_rate"] == 0.0
    assert ideas[0]["idea_score"] == pytest.approx(0.4)


def test_trade_ideas_normalize_fractional_win_rate_for_ranking_and_display():
    from app.services.trading.daily_playbook import _generate_trade_ideas

    pat = _pattern(
        id=8,
        name="Fractional OOS evidence",
        oos_win_rate=0.55,
        win_rate=0.2,
        avg_return_pct=0.0,
    )

    ideas = _generate_trade_ideas(_PatternDb([pat]), user_id=None, capital=100_000)

    assert ideas[0]["pattern_id"] == 8
    assert ideas[0]["oos_win_rate"] == 55.0
    assert ideas[0]["idea_score"] == pytest.approx(0.33)
