from collections import OrderedDict
from types import SimpleNamespace

from app.services.trading import dynamic_priors


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):
        raise AssertionError("cache pruning should evict from the oldest entry")


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

    assert "pnl /" in db.sql
    assert "entry_price" in db.sql
    assert "quantity" in db.sql
    assert "asset_kind" in db.sql
    assert db.params == {"ld": 7}


def test_population_win_rate_excludes_unrealized_closed_rows() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session(SimpleNamespace(n=2, ar=None, wr=0.5))

    assert dynamic_priors.population_win_rate(db, lookback_days=7) == 0.5

    assert "COALESCE(pnl, 0)" not in db.sql
    assert "pnl IS NOT NULL" in db.sql
    assert "entry_price > 0" in db.sql
    assert "quantity > 0" in db.sql
    assert db.params == {"ld": 7}


def test_dynamic_prior_cache_hit_refreshes_recency(monkeypatch) -> None:
    monkeypatch.setattr(dynamic_priors.time, "time", lambda: 100.0)
    dynamic_priors._CACHE.clear()
    dynamic_priors._CACHE["a"] = (100.0, 1)
    dynamic_priors._CACHE["b"] = (100.0, 2)

    assert dynamic_priors._cache_get("a") == 1

    assert list(dynamic_priors._CACHE) == ["b", "a"]


def test_dynamic_prior_cache_caps_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(dynamic_priors.time, "time", lambda: 100.0)
    monkeypatch.setattr(dynamic_priors, "_CACHE_MAX", 2)
    cache = _NoSnapshotOrderedDict([("a", (100.0, 1)), ("b", (100.0, 2))])
    monkeypatch.setattr(dynamic_priors, "_CACHE", cache)

    dynamic_priors._cache_set("c", 3)

    assert list(cache) == ["b", "c"]
