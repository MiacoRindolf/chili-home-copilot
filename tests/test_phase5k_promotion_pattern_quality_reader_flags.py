from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.services.trading import pattern_cohort_promote, pattern_quality_score


class _FakeFetchAllDb:
    def __init__(self) -> None:
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params or {}
        return self

    def fetchall(self):
        return []


class _FakeMappingsResult:
    def __init__(self, db):
        self.db = db

    def mappings(self):
        return self

    def all(self):
        return []


class _FakeMappingsDb:
    def __init__(self) -> None:
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params or {}
        return _FakeMappingsResult(self)


def test_pattern_quality_source_relation_defaults_to_compat(monkeypatch):
    monkeypatch.delenv(pattern_quality_score.PHASE5K_PATTERN_QUALITY_ENV, raising=False)

    assert pattern_quality_score._pattern_quality_source_relation() == "trading_trades"


def test_pattern_quality_source_relation_honors_typed_settings(monkeypatch):
    monkeypatch.setenv(pattern_quality_score.PHASE5K_PATTERN_QUALITY_ENV, "on")

    assert (
        pattern_quality_score._pattern_quality_source_relation(
            settings_=SimpleNamespace(chili_phase5k_pattern_quality_use_envelopes=True),
        )
        == "trading_management_envelopes"
    )


def test_pattern_quality_env_alias_flows_through_settings(monkeypatch):
    monkeypatch.setenv(pattern_quality_score.PHASE5K_PATTERN_QUALITY_ENV, "on")
    s = Settings(_env_file=None)

    assert s.chili_phase5k_pattern_quality_use_envelopes is True
    assert (
        pattern_quality_score._pattern_quality_source_relation(settings_=s)
        == "trading_management_envelopes"
    )


def test_pattern_quality_env_does_not_override_explicit_settings_object(monkeypatch):
    monkeypatch.setenv(pattern_quality_score.PHASE5K_PATTERN_QUALITY_ENV, "on")

    assert (
        pattern_quality_score._pattern_quality_source_relation(
            settings_=SimpleNamespace(chili_phase5k_pattern_quality_use_envelopes=False),
        )
        == "trading_trades"
    )


def test_pattern_quality_realized_map_uses_selected_relation():
    db = _FakeFetchAllDb()

    out = pattern_quality_score._load_realized_pnl_map(
        db,
        90,
        use_envelopes=True,
    )

    assert out == {}
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params == {"window_days": 90}


def test_cohort_promote_source_relation_defaults_to_compat(monkeypatch):
    monkeypatch.delenv(pattern_cohort_promote.PHASE5K_COHORT_PROMOTE_ENV, raising=False)

    assert pattern_cohort_promote._cohort_promote_source_relation() == "trading_trades"


def test_cohort_promote_source_relation_honors_typed_settings(monkeypatch):
    monkeypatch.setenv(pattern_cohort_promote.PHASE5K_COHORT_PROMOTE_ENV, "true")

    assert (
        pattern_cohort_promote._cohort_promote_source_relation(
            settings_=SimpleNamespace(chili_phase5k_cohort_promote_use_envelopes=True),
        )
        == "trading_management_envelopes"
    )


def test_cohort_promote_env_alias_flows_through_settings(monkeypatch):
    monkeypatch.setenv(pattern_cohort_promote.PHASE5K_COHORT_PROMOTE_ENV, "true")
    s = Settings(_env_file=None)

    assert s.chili_phase5k_cohort_promote_use_envelopes is True
    assert (
        pattern_cohort_promote._cohort_promote_source_relation(settings_=s)
        == "trading_management_envelopes"
    )


def test_cohort_promote_env_does_not_override_explicit_settings_object(monkeypatch):
    monkeypatch.setenv(pattern_cohort_promote.PHASE5K_COHORT_PROMOTE_ENV, "true")

    assert (
        pattern_cohort_promote._cohort_promote_source_relation(
            settings_=SimpleNamespace(chili_phase5k_cohort_promote_use_envelopes=False),
        )
        == "trading_trades"
    )


def test_cohort_promote_select_uses_envelope_relation():
    db = _FakeMappingsDb()

    out = pattern_cohort_promote.select_cohort_candidates(
        db,
        settings_=SimpleNamespace(chili_phase5k_cohort_promote_use_envelopes=True),
    )

    assert out == []
    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert db.params["window_days"] == 90
