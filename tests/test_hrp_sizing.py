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
    assert "row_number() OVER" in db.sql
    assert "PARTITION BY ticker" in db.sql
    assert "rn <= :return_window_days" in db.sql
    assert "last_price" not in db.sql
    assert db.params["symbols"] == ["AAA", "BBB"]
    assert db.params["return_window_days"] == hrp_sizing._RETURN_WINDOW_DAYS


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


def test_fetch_returns_matrix_requires_enough_prices_for_min_return_observations():
    start = datetime(2026, 1, 1)
    rows = []
    for idx in range(hrp_sizing._MIN_OBS_PER_SYMBOL):
        ts = start + timedelta(days=idx)
        rows.append(("AAA", ts, 100.0 + idx))
        rows.append(("BBB", ts, 80.0 + idx * 0.5))
    db = _FakeDb(rows)

    matrix, kept, meta = hrp_sizing._fetch_returns_matrix(
        db,
        ["AAA", "BBB"],
        now=start + timedelta(days=hrp_sizing._MIN_OBS_PER_SYMBOL + 1),
    )

    assert matrix is None
    assert kept == []
    assert meta["hrp_skip"] == "insufficient_returns_history"
    assert meta["insufficient_history_symbol_count"] == 2


def test_fetch_returns_matrix_dedupes_symbols_before_query():
    start = datetime(2026, 1, 1)
    rows = []
    for idx in range(35):
        ts = start + timedelta(days=idx)
        rows.append(("AAA", ts, 100.0 + idx))
        rows.append(("BBB", ts, 80.0 + idx * 0.5))
    db = _FakeDb(rows)

    matrix, kept, meta = hrp_sizing._fetch_returns_matrix(
        db,
        ["aaa", "BBB", "AAA"],
        now=start + timedelta(days=36),
    )

    assert matrix is not None
    assert kept == ["AAA", "BBB"]
    assert meta["kept_symbols"] == ["AAA", "BBB"]
    assert db.params["symbols"] == ["AAA", "BBB"]


def test_fetch_returns_matrix_skips_query_after_symbol_dedupe_below_two():
    db = _FakeDb([])

    matrix, kept, meta = hrp_sizing._fetch_returns_matrix(db, ["aaa", "AAA"])

    assert matrix is None
    assert kept == []
    assert meta["hrp_skip"] == "fewer_than_2_symbols"
    assert db.sql == ""


def test_active_position_symbols_uses_user_scoped_reader_without_or_predicate():
    db = _FakeDb([("AAA",), ("BBB",)])

    result = hrp_sizing._fetch_active_position_symbols(db, user_id=7)

    assert result == ["AAA", "BBB"]
    assert "FROM trading_management_envelopes" in db.sql
    assert "UPPER(BTRIM(ticker)) AS ticker" in db.sql
    assert "user_id = :uid" in db.sql
    assert "ticker IS NOT NULL" in db.sql
    assert "BTRIM(ticker) <> ''" in db.sql
    assert "OR :uid IS NULL" not in db.sql
    assert db.params == {"uid": 7}


def test_active_position_symbols_global_reader_omits_user_filter():
    db = _FakeDb([("AAA",)])

    result = hrp_sizing._fetch_active_position_symbols(db, user_id=None)

    assert result == ["AAA"]
    assert "FROM trading_management_envelopes" in db.sql
    assert "UPPER(BTRIM(ticker)) AS ticker" in db.sql
    assert "user_id" not in db.sql
    assert "ticker IS NOT NULL" in db.sql
    assert "BTRIM(ticker) <> ''" in db.sql
    assert db.params == {}


def test_decide_position_size_skips_hrp_reads_for_non_positive_equity(monkeypatch):
    db = _FakeDb([])

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("HRP reads should be skipped for non-positive equity")

    monkeypatch.setattr(hrp_sizing, "_fetch_active_position_symbols", fail_fetch)
    monkeypatch.setattr(hrp_sizing, "_fetch_returns_matrix", fail_fetch)

    decision = hrp_sizing.decide_position_size(
        db,
        "AAA",
        0.0,
        user_id=7,
        persist_log=False,
    )

    assert decision.chosen_sizing == "naive"
    assert decision.naive_size_usd == 0.0
    assert decision.hrp_size_usd is None
    assert decision.meta["hrp_skip"] == "non_positive_account_equity"
    assert db.sql == ""


def test_decide_position_size_dedupes_universe_in_stable_order(monkeypatch):
    captured = {}

    monkeypatch.setattr(hrp_sizing, "_fetch_active_position_symbols", lambda *_args: ["bbb", "AAA"])

    def fake_fetch_returns(_db, symbols):
        captured["symbols"] = symbols
        return None, [], {"hrp_skip": "insufficient_returns_history"}

    monkeypatch.setattr(hrp_sizing, "_fetch_returns_matrix", fake_fetch_returns)

    decision = hrp_sizing.decide_position_size(
        _FakeDb([]),
        "aaa",
        1000.0,
        user_id=7,
        persist_log=False,
    )

    assert captured["symbols"] == ["BBB", "AAA"]
    assert decision.meta["hrp_skip"] == "insufficient_returns_history"
