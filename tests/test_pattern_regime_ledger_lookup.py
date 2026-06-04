from datetime import date

from app.services.trading.pattern_regime_ledger_lookup import load_resolved_context


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, result_sets):
        self._result_sets = list(result_sets)
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), dict(params)))
        return _Rows(self._result_sets.pop(0))


def test_load_resolved_context_uses_bounded_lateral_latest_lookup() -> None:
    session = _FakeSession(
        [
            [
                (
                    "macro_regime",
                    "risk_on",
                    date(2026, 6, 4),
                    14,
                    20,
                    0.6,
                    0.02,
                    0.01,
                    1.4,
                    True,
                )
            ],
            [],
        ]
    )

    ctx = load_resolved_context(
        session,
        pattern_id=1246,
        as_of_date=date(2026, 6, 4),
        max_staleness_days=14,
        dimensions=("macro_regime", "ticker_regime"),
    )

    first_sql, first_params = session.calls[0]
    assert "JOIN LATERAL" in first_sql
    assert "LIMIT 1" in first_sql
    assert "DISTINCT ON" not in first_sql
    assert first_params["dims"] == ["macro_regime", "ticker_regime"]
    assert ctx.n_confident_dimensions == 1
    assert ctx.cells_by_dimension["macro_regime"].regime_label == "risk_on"
    assert ctx.unavailable_dimensions == ("ticker_regime",)
