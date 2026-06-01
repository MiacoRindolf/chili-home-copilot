from types import SimpleNamespace

import pytest

from app.services.trading import dynamic_priors


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Session:
    sql = ""

    def __init__(self, row=None):
        self.row = row or SimpleNamespace(n=1, ar=16.0, wr=None)

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Result(self.row)


def test_population_avg_return_pct_normalizes_from_realized_pnl() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session()

    assert dynamic_priors.population_avg_return_pct(db, lookback_days=7) == 16.0

    assert "realized_return_frac" in db.sql
    assert "COUNT(realized_return_frac) AS n" in db.sql
    assert "AVG(realized_return_frac * 100.0) AS ar" in db.sql
    assert "pnl /" in db.sql
    assert "entry_price" in db.sql
    assert "quantity" in db.sql
    assert "asset_kind" in db.sql
    assert db.params == {"ld": 7, "user_id": None}


def test_population_win_rate_excludes_unrealized_closed_rows() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session(SimpleNamespace(n=2, ar=None, wr=0.5))

    assert dynamic_priors.population_win_rate(db, lookback_days=7) == 0.5

    assert "realized_return_frac" in db.sql
    assert "COUNT(realized_return_frac) AS n" in db.sql
    assert "realized_return_frac > 0" in db.sql
    assert "CASE WHEN pnl > 0" not in db.sql
    assert "COALESCE(pnl, 0)" not in db.sql
    assert "pnl IS NOT NULL" in db.sql
    assert "entry_price > 0" in db.sql
    assert "quantity > 0" in db.sql
    assert db.params == {"ld": 7, "user_id": None}


def test_population_win_rate_can_scope_to_user_id_zero() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session(SimpleNamespace(n=2, ar=None, wr=0.5))

    assert dynamic_priors.population_win_rate(db, lookback_days=7, user_id=0) == 0.5

    assert "(:user_id IS NULL OR user_id = :user_id)" in db.sql
    assert db.params == {"ld": 7, "user_id": 0}


def test_population_avg_return_pct_can_scope_to_user_id_zero() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session()

    assert dynamic_priors.population_avg_return_pct(db, lookback_days=7, user_id=0) == 16.0

    assert "(:user_id IS NULL OR user_id = :user_id)" in db.sql
    assert db.params == {"ld": 7, "user_id": 0}


def test_population_prior_lookback_rejects_boolean_and_fractional_zero() -> None:
    dynamic_priors._CACHE.clear()
    win_db = _Session(SimpleNamespace(n=2, ar=None, wr=0.5))
    avg_db = _Session()

    assert dynamic_priors.population_win_rate(win_db, lookback_days=True) == 0.5
    assert win_db.params == {"ld": 90, "user_id": None}

    assert dynamic_priors.population_avg_return_pct(avg_db, lookback_days=0.5) == 16.0
    assert avg_db.params == {"ld": 90, "user_id": None}


def test_population_prior_rejects_invalid_user_scope() -> None:
    dynamic_priors._CACHE.clear()
    win_db = _Session(SimpleNamespace(n=2, ar=None, wr=0.5))
    avg_db = _Session()

    assert dynamic_priors.population_win_rate(win_db, lookback_days=7, user_id=True) is None
    assert win_db.sql == ""

    assert dynamic_priors.population_avg_return_pct(avg_db, lookback_days=7, user_id=0.5) is None
    assert avg_db.sql == ""


def test_population_prior_rejects_malformed_database_values() -> None:
    dynamic_priors._CACHE.clear()

    for row in (
        SimpleNamespace(n=True, ar=None, wr=0.5),
        SimpleNamespace(n=2, ar=None, wr=True),
        SimpleNamespace(n=2, ar=None, wr=1.2),
        SimpleNamespace(n=2, ar=None, wr=float("nan")),
    ):
        assert dynamic_priors.population_win_rate(_Session(row), lookback_days=7) is None

    for row in (
        SimpleNamespace(n=True, ar=16.0, wr=None),
        SimpleNamespace(n=2, ar=True, wr=None),
        SimpleNamespace(n=2, ar=float("nan"), wr=None),
    ):
        assert dynamic_priors.population_avg_return_pct(_Session(row), lookback_days=7) is None


def test_bayesian_pattern_win_rate_can_scope_population_prior_to_user_id_zero() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session(SimpleNamespace(n=10, ar=None, wr=0.6))

    out = dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=1,
        pattern_n=2,
        user_id=0,
    )

    assert out == pytest.approx(4.0 / 7.0)
    assert db.params == {"ld": 90, "user_id": 0}


def test_bayesian_pattern_win_rate_rejects_boolean_and_fractional_counts() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session(SimpleNamespace(n=10, ar=None, wr=0.6))

    assert dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=True,
        pattern_n=2,
    ) is None
    assert dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=0.5,
        pattern_n=2,
    ) is None
    assert dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=1,
        pattern_n=2.5,
    ) is None
    assert dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=1,
        pattern_n=2,
        prior_strength=True,
    ) is None
    assert dynamic_priors.bayesian_pattern_win_rate(
        db,
        pattern_wins=1,
        pattern_n=2,
        prior_strength=2.5,
    ) is None


def test_bayesian_pattern_confidence_rejects_bogus_counts_and_zero_denominator() -> None:
    assert dynamic_priors.bayesian_pattern_confidence(True) is None
    assert dynamic_priors.bayesian_pattern_confidence(0.5) is None
    assert dynamic_priors.bayesian_pattern_confidence(2.5) is None
    assert dynamic_priors.bayesian_pattern_confidence(0, prior_strength=0) is None
    assert dynamic_priors.bayesian_pattern_confidence(2, prior_strength=2.5) is None
    assert dynamic_priors.bayesian_pattern_confidence(2, prior_strength=0) == 1.0
