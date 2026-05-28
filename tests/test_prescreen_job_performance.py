from __future__ import annotations

from app.models.trading import PrescreenCandidate
from app.services.trading.prescreen_job import _global_candidates_by_ticker_norm


class _FakeQuery:
    def __init__(self, rows: list[PrescreenCandidate]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[PrescreenCandidate]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[PrescreenCandidate]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, model: object) -> _FakeQuery:
        assert model is PrescreenCandidate
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_global_candidates_by_ticker_norm_batches_lookup() -> None:
    first = PrescreenCandidate(ticker="AAPL", ticker_norm="AAPL", asset_universe="stock")
    duplicate = PrescreenCandidate(ticker="AAPL", ticker_norm="AAPL", asset_universe="stock")
    crypto = PrescreenCandidate(ticker="BTC-USD", ticker_norm="BTC-USD", asset_universe="crypto")
    db = _FakeSession([first, duplicate, crypto])

    result = _global_candidates_by_ticker_norm(db, {"AAPL", "BTC-USD"})

    assert result == {"AAPL": first, "BTC-USD": crypto}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 2


def test_global_candidates_by_ticker_norm_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _global_candidates_by_ticker_norm(db, set()) == {}
    assert db.query_calls == 0
