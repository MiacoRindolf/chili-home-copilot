from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.momentum_neural import family_regime_stats


def _row(
    *,
    family: str,
    return_bps: float,
    volatility_regime: str = "high",
    session_label: str = "trend",
    entry: bool = True,
):
    snapshot = {
        "volatility_regime": volatility_regime,
        "session_label": session_label,
    }
    out = SimpleNamespace(
        return_bps=return_bps,
        entry_regime_snapshot_json=snapshot if entry else {},
        regime_snapshot_json=snapshot,
    )
    var = SimpleNamespace(family=family)
    return out, var


def _column_row(
    *,
    family: str,
    return_bps: float,
    volatility_regime: str = "high",
    session_label: str = "trend",
    entry: bool = True,
):
    snapshot = {
        "volatility_regime": volatility_regime,
        "session_label": session_label,
    }
    return (
        return_bps,
        snapshot if entry else {},
        snapshot,
        family,
    )


def test_target_family_regime_summary_matches_aggregate_bucket() -> None:
    rows = [
        _row(family="breakout", return_bps=-30),
        _row(family="breakout", return_bps=-20),
        _row(family="breakout", return_bps=5),
        _row(family="meanrev", return_bps=100),
        _row(family="breakout", return_bps=-99, volatility_regime="low"),
        _row(family="breakout", return_bps=-99, session_label="chop"),
    ]

    target = family_regime_stats._target_family_regime_summary(
        rows,
        family_id="Breakout",
        volatility_regime="high",
        session_label="trend",
    )

    assert target == {
        "family_id": "breakout",
        "volatility_regime": "high",
        "session_label": "trend",
        "n": 3,
        "win_rate": 1 / 3,
        "mean_return_bps": -15.0,
    }


def test_bucket_summary_consumes_returns_once() -> None:
    class OneShotReturns:
        def __init__(self, values: list[float]):
            self.values = values
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("returns were scanned more than once")
            yield from self.values

    returns = OneShotReturns([-5.0, 0.0, 10.0])

    assert family_regime_stats._bucket_summary(
        family_id="breakout",
        volatility_regime="high",
        session_label="trend",
        returns=returns,
    ) == {
        "family_id": "breakout",
        "volatility_regime": "high",
        "session_label": "trend",
        "n": 3,
        "win_rate": 1 / 3,
        "mean_return_bps": 5.0 / 3.0,
    }
    assert returns.iterations == 1


def test_aggregate_family_regime_performance_groups_bucket_rows() -> None:
    class FakeQuery:
        def __init__(self, rows):
            self.rows = rows

        def join(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return self.rows

    class FakeDb:
        def __init__(self, rows):
            self.rows = rows
            self.full_orm_query_count = 0
            self.column_query_count = 0

        def query(self, *args):
            if len(args) == 2:
                self.full_orm_query_count += 1
            if len(args) == 4:
                self.column_query_count += 1
            return FakeQuery(self.rows)

    rows = [
        _column_row(family="breakout", return_bps=20),
        _column_row(family="breakout", return_bps=-10),
        _column_row(family="meanrev", return_bps=-30),
    ]

    db = FakeDb(rows)
    out = family_regime_stats.aggregate_family_regime_performance(db)

    assert db.full_orm_query_count == 0
    assert db.column_query_count == 1
    assert out == [
        {
            "family_id": "breakout",
            "volatility_regime": "high",
            "session_label": "trend",
            "n": 2,
            "win_rate": 0.5,
            "mean_return_bps": 5.0,
        },
        {
            "family_id": "meanrev",
            "volatility_regime": "high",
            "session_label": "trend",
            "n": 1,
            "win_rate": 0.0,
            "mean_return_bps": -30.0,
        },
    ]


def test_target_family_regime_summary_uses_regime_snapshot_fallback() -> None:
    rows = [_row(family="breakout", return_bps=-20, entry=False)]

    target = family_regime_stats._target_family_regime_summary(
        rows,
        family_id="breakout",
        volatility_regime="high",
        session_label="trend",
    )

    assert target is not None
    assert target["n"] == 1
    assert target["mean_return_bps"] == -20.0


def test_target_family_regime_summary_returns_none_when_bucket_missing() -> None:
    rows = [
        _row(family="meanrev", return_bps=-20),
        _row(family="breakout", return_bps=-20, volatility_regime="low"),
    ]

    target = family_regime_stats._target_family_regime_summary(
        rows,
        family_id="breakout",
        volatility_regime="high",
        session_label="trend",
    )

    assert target is None


def test_column_target_family_regime_summary_matches_object_summary() -> None:
    rows = [
        _column_row(family="breakout", return_bps=-30),
        _column_row(family="breakout", return_bps=10),
        _column_row(family="meanrev", return_bps=100),
        _column_row(family="breakout", return_bps=-99, volatility_regime="low"),
    ]

    target = family_regime_stats._target_family_regime_summary_from_column_rows(
        rows,
        family_id="breakout",
        volatility_regime="high",
        session_label="trend",
    )

    assert target == {
        "family_id": "breakout",
        "volatility_regime": "high",
        "session_label": "trend",
        "n": 2,
        "win_rate": 0.5,
        "mean_return_bps": -10.0,
    }


def test_outcome_family_columns_uses_regime_snapshot_fallback() -> None:
    value, fam, vol, session = family_regime_stats._outcome_family_columns(
        _column_row(family="breakout", return_bps=-20, entry=False)
    )

    assert value == -20.0
    assert fam == "breakout"
    assert vol == "high"
    assert session == "trend"
