from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.trading import learning


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeLearningDb:
    def __init__(self, fakeouts, winners):
        self._queries = [_FakeQuery(fakeouts), _FakeQuery(winners)]

    def query(self, _model):
        return self._queries.pop(0)


def _alert(indicators):
    return SimpleNamespace(
        indicator_snapshot=json.dumps(indicators),
        signals_snapshot=json.dumps([]),
    )


def test_fakeout_mining_preserves_zero_volume_and_width(monkeypatch):
    fakeouts = [_alert({"rvol": 0.0, "bb_width": 0.0}) for _ in range(3)]
    winners = [_alert({"rvol": 1.4, "bb_width": 0.08}) for _ in range(3)]
    saved = []

    def _save_insight(_db, _user_id, description, **kwargs):
        saved.append({"description": description, **kwargs})

    monkeypatch.setattr(learning, "save_insight", _save_insight)
    monkeypatch.setattr(learning, "log_learning_event", lambda *_args, **_kwargs: None)

    result = learning.mine_fakeout_patterns(
        _FakeLearningDb(fakeouts, winners),
        user_id=7,
    )

    descriptions = [row["description"] for row in saved]
    assert result["patterns_found"] == 2
    assert any("RVOL < 1.0" in desc for desc in descriptions)
    assert any("BB width narrow (<0.02)" in desc for desc in descriptions)
