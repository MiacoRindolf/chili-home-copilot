from types import SimpleNamespace

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
