from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
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

    assert (
        portfolio_risk._portfolio_risk_source_relation(
            settings_=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=False)
        )
        == "trading_trades"
    )


def test_portfolio_risk_phase5k_flag_is_typed_default_false(monkeypatch):
    monkeypatch.delenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, raising=False)

    settings = Settings(_env_file=None)

    assert settings.chili_phase5k_portfolio_risk_use_envelopes is False


def test_portfolio_risk_phase5k_env_alias_flows_through_settings(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")
    settings = Settings(_env_file=None)

    assert settings.chili_phase5k_portfolio_risk_use_envelopes is True
    assert (
        portfolio_risk._portfolio_risk_source_relation(settings_=settings)
        == "trading_management_envelopes"
    )


def test_portfolio_risk_env_does_not_override_explicit_settings_object(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")

    assert (
        portfolio_risk._portfolio_risk_source_relation(
            settings_=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=False)
        )
        == "trading_trades"
    )


def test_portfolio_risk_reader_flag_has_no_direct_env_read():
    source = Path(portfolio_risk.__file__).read_text()

    assert "import os" not in source
    assert "os.environ" not in source
    assert "os.getenv" not in source


def test_portfolio_risk_source_relation_explicit_override(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")

    assert (
        portfolio_risk._portfolio_risk_source_relation(
            use_envelopes=False,
            settings_=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=True),
        )
        == "trading_trades"
    )
    assert (
        portfolio_risk._portfolio_risk_source_relation(
            use_envelopes=True,
            settings_=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=False),
        )
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


def test_monthly_dd_threshold_uses_typed_settings_relation():
    db = _FakeDb()

    threshold, n_obs = portfolio_risk._monthly_dd_threshold(
        db,
        1,
        settings_obj=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=True),
    )

    assert threshold is None
    assert n_obs == 0
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql


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


def test_monthly_total_pnl_env_does_not_override_typed_settings(monkeypatch):
    monkeypatch.setenv(portfolio_risk.PHASE5K_PORTFOLIO_RISK_ENV, "true")
    db = _FakeDb(scalar_value=-7.25)

    out = portfolio_risk._monthly_total_pnl(
        db,
        None,
        settings_obj=SimpleNamespace(chili_phase5k_portfolio_risk_use_envelopes=False),
    )

    assert out == -7.25
    assert "FROM trading_trades" in db.sql
    assert "FROM trading_management_envelopes" not in db.sql
