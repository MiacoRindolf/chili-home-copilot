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
        self.queried = None

    def query(self, *_args, **_kwargs):
        self.queried = _args
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


def test_mine_proposals_reads_snapshot_metric_columns_only(monkeypatch) -> None:
    rows = [
        (
            0.40 if i < 30 else -0.05,
            {"bucket": "winner" if i < 30 else "base"},
            100.0,
        )
        for i in range(60)
    ]
    db = _Db(rows)

    def flat_snapshot(indicator_data, _close_price):
        if indicator_data["bucket"] == "winner":
            return {
                "rsi_14": 30.0,
                "macd_hist": 1.0,
                "bb_pct_b": 0.1,
                "adx": 30.0,
                "stoch_k": 10.0,
            }
        return {
            "rsi_14": 50.0,
            "macd_hist": -1.0,
            "bb_pct_b": 0.8,
            "adx": 5.0,
            "stoch_k": 70.0,
        }

    monkeypatch.setattr(
        "app.services.trading.learning_predictions._indicator_data_to_flat_snapshot",
        flat_snapshot,
    )

    proposals = sph.mine_proposals_from_snapshots(
        db,
        max_proposals=2,
        min_samples=5,
        min_lift_pct=0.01,
        snapshot_limit=60,
    )

    assert [getattr(col, "key", None) for col in db.queried] == [
        "future_return_5d",
        "indicator_data",
        "close_price",
    ]
    assert len(proposals) == 2
    assert all(proposal["name"].startswith("Stat_") for proposal in proposals)


def test_snapshot_proposal_values_supports_object_tuple_and_mapping_rows() -> None:
    obj = SimpleNamespace(future_return_5d=1.0, indicator_data={"a": 1}, close_price=10.0)
    mapping = {
        "future_return_5d": 2.0,
        "indicator_data": {"b": 2},
        "close_price": 20.0,
    }

    assert sph._snapshot_proposal_values(obj) == (1.0, {"a": 1}, 10.0)
    assert sph._snapshot_proposal_values((3.0, {"c": 3}, 30.0)) == (3.0, {"c": 3}, 30.0)
    assert sph._snapshot_proposal_values(mapping) == (2.0, {"b": 2}, 20.0)
