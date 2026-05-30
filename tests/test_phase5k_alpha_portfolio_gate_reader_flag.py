from __future__ import annotations

from app.services.trading import alpha_portfolio_gate


class _FakeMappingsResult:
    def __init__(self, db):
        self.db = db

    def mappings(self):
        return self

    def all(self):
        return []


class _FakeDb:
    def __init__(self) -> None:
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params or {}
        return _FakeMappingsResult(self)


def test_alpha_portfolio_gate_source_relation_defaults_to_compat(monkeypatch):
    monkeypatch.delenv(
        alpha_portfolio_gate.PHASE5K_ALPHA_PORTFOLIO_GATE_ENV,
        raising=False,
    )

    assert alpha_portfolio_gate._alpha_portfolio_gate_source_relation() == "trading_trades"


def test_alpha_portfolio_gate_source_relation_honors_env(monkeypatch):
    monkeypatch.setenv(
        alpha_portfolio_gate.PHASE5K_ALPHA_PORTFOLIO_GATE_ENV,
        "true",
    )

    assert (
        alpha_portfolio_gate._alpha_portfolio_gate_source_relation()
        == "trading_management_envelopes"
    )


def test_alpha_portfolio_gate_source_relation_explicit_override(monkeypatch):
    monkeypatch.setenv(
        alpha_portfolio_gate.PHASE5K_ALPHA_PORTFOLIO_GATE_ENV,
        "true",
    )

    assert (
        alpha_portfolio_gate._alpha_portfolio_gate_source_relation(
            use_envelopes=False,
        )
        == "trading_trades"
    )
    assert (
        alpha_portfolio_gate._alpha_portfolio_gate_source_relation(
            use_envelopes=True,
        )
        == "trading_management_envelopes"
    )


def test_load_pattern_rows_uses_envelope_relation():
    db = _FakeDb()

    out = alpha_portfolio_gate._load_pattern_rows(
        db,
        pattern_id=585,
        realized_window_days=90,
        use_envelopes=True,
    )

    assert out == []
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"pattern_id": 585, "window_days": 90}
