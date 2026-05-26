from types import SimpleNamespace

from app.services.trading import dynamic_priors


class _Result:
    def fetchone(self):
        return SimpleNamespace(n=1, ar=16.0)


class _Session:
    sql = ""

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return _Result()


def test_population_avg_return_pct_normalizes_from_realized_pnl() -> None:
    dynamic_priors._CACHE.clear()
    db = _Session()

    assert dynamic_priors.population_avg_return_pct(db, lookback_days=7) == 16.0

    assert "pnl /" in db.sql
    assert "entry_price" in db.sql
    assert "quantity" in db.sql
    assert "asset_kind" in db.sql
    assert db.params == {"ld": 7}
