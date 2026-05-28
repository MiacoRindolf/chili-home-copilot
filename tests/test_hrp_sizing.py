from datetime import datetime, timedelta

from app.services.trading import hrp_sizing


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params
        return _Rows(self.rows)


def test_fetch_returns_matrix_uses_snapshot_close_price_column():
    start = datetime(2026, 1, 1)
    rows = []
    for idx in range(35):
        ts = start + timedelta(days=idx)
        rows.append(("AAA", ts, 100.0 + idx))
        rows.append(("BBB", ts, 80.0 + idx * 0.5))
    db = _FakeDb(rows)

    matrix, kept, meta = hrp_sizing._fetch_returns_matrix(
        db,
        ["AAA", "BBB"],
        now=start + timedelta(days=36),
    )

    assert matrix is not None
    assert kept == ["AAA", "BBB"]
    assert meta["kept_symbols"] == ["AAA", "BBB"]
    assert meta["bar_interval"] == "1d"
    assert "bar_interval = '1d'" in db.sql
    assert "close_price AS price" in db.sql
    assert "last_price" not in db.sql
    assert db.params["symbols"] == ["AAA", "BBB"]


def test_fetch_returns_matrix_blocks_stale_snapshot_history():
    now = datetime(2026, 1, 20)
    start = now - timedelta(days=45)
    rows = []
    for idx in range(35):
        ts = start + timedelta(days=idx)
        rows.append(("AAA", ts, 100.0 + idx))
        rows.append(("BBB", ts, 80.0 + idx * 0.5))
    db = _FakeDb(rows)

    matrix, kept, meta = hrp_sizing._fetch_returns_matrix(
        db,
        ["AAA", "BBB"],
        now=now,
        max_staleness_days=5,
    )

    assert matrix is None
    assert kept == []
    assert meta["hrp_skip"] == "returns_history_stale"
    assert meta["stale_symbol_count"] == 2
    assert {row["symbol"] for row in meta["stale_symbols"]} == {"AAA", "BBB"}
