from __future__ import annotations

from app.services.trading import portfolio_risk


class _FakeDb:
    def __init__(self, scalar_value=0.0) -> None:
        self.sql = ""
        self.params = None
        self.scalar_value = scalar_value

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params or {}
        return self

    def fetchall(self):
        return []

    def scalar(self):
        return self.scalar_value


def test_portfolio_risk_source_relation_defaults_to_compat(monkeypatch):
    monkeypatch.delenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, raising=False)

    assert portfolio_risk._portfolio_risk_source_relation() == "trading_trades"


def test_portfolio_risk_source_relation_honors_env(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")

    assert (
        portfolio_risk._portfolio_risk_source_relation()
        == "trading_management_envelopes"
    )


def test_portfolio_risk_source_relation_explicit_override(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")

    assert (
        portfolio_risk._portfolio_risk_source_relation(use_envelopes=False)
        == "trading_trades"
    )
    assert (
        portfolio_risk._portfolio_risk_source_relation(use_envelopes=True)
        == "trading_management_envelopes"
    )


def test_monthly_dd_threshold_uses_envelope_relation():
    db = _FakeDb()

    threshold, n_obs = portfolio_risk._monthly_dd_threshold(
        db,
        1,
        use_envelopes=True,
    )

    assert threshold is None
    assert n_obs == 0
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"uid": 1}


def test_monthly_attributed_pnl_uses_envelope_relation():
    db = _FakeDb(scalar_value=12.5)

    out = portfolio_risk._monthly_attributed_pnl(db, 1, use_envelopes=True)

    assert out == 12.5
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"uid": 1}


def test_portfolio_dd_threshold_uses_envelope_relation():
    db = _FakeDb()

    threshold, n_obs = portfolio_risk._portfolio_dd_threshold(
        db,
        None,
        use_envelopes=True,
    )

    assert threshold is None
    assert n_obs == 0
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"uid": None}


def test_monthly_total_pnl_uses_envelope_relation():
    db = _FakeDb(scalar_value=-7.25)

    out = portfolio_risk._monthly_total_pnl(db, None, use_envelopes=True)

    assert out == -7.25
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"uid": None}
