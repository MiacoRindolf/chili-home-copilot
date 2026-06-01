from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import statistical_pattern_hypotheses as sph


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return _Rows(self._rows)


def test_mine_proposals_reuses_candidate_mean_without_rescanning(monkeypatch) -> None:
    rows = [
        SimpleNamespace(
            future_return_5d=0.10 + (i % 5) / 100.0,
            indicator_data={"ignored": True},
            close_price=100.0,
        )
        for i in range(120)
    ]
    candidate_count = len(sph._STAT_CANDIDATES)
    calls = 0

    def flat_snapshot(_indicator_data, _close_price):
        return {
            "rsi_14": 30.0,
            "macd_hist": 1.0,
            "bb_pct_b": 0.1,
            "adx": 30.0,
            "stoch_k": 10.0,
        }

    def matches_all(_flat, _conditions):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "app.services.trading.learning_predictions._indicator_data_to_flat_snapshot",
        flat_snapshot,
    )
    monkeypatch.setattr(sph, "_flat_matches_all", matches_all)

    proposals = sph.mine_proposals_from_snapshots(
        _Db(rows),
        max_proposals=5,
        min_samples=5,
        min_lift_pct=-1.0,
        snapshot_limit=120,
    )

    assert len(proposals) == candidate_count
    assert calls == candidate_count * len(rows)


def test_conditions_fingerprint_uses_canonical_json() -> None:
    first = [{"indicator": "rsi_14", "op": "<", "value": 35}]
    second = [{"value": 35, "op": "<", "indicator": "rsi_14"}]

    assert sph._conditions_fingerprint(first) == sph._conditions_fingerprint(second)
