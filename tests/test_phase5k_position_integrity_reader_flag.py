from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import config as app_config
from app.config import Settings
from app.services.trading import position_integrity


class _FakeMappings:
    def all(self):
        return []


class _FakeResult:
    rowcount = 0

    def mappings(self):
        return _FakeMappings()

    def fetchall(self):
        return []


class _FakeDb:
    def __init__(self) -> None:
        self.sqls: list[str] = []
        self.params: list[dict] = []

    def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        self.params.append(params or {})
        return _FakeResult()


def test_position_integrity_source_relation_defaults_to_compat(monkeypatch):
    monkeypatch.delenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, raising=False)

    assert (
        position_integrity._position_integrity_source_relation(
            settings_=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=False)
        )
        == "trading_trades"
    )


def test_position_integrity_phase5k_flag_is_typed_default_false(monkeypatch):
    monkeypatch.delenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, raising=False)

    settings = Settings(_env_file=None)

    assert settings.chili_phase5k_position_integrity_use_envelopes is False


def test_position_integrity_phase5k_env_alias_flows_through_settings(monkeypatch):
    monkeypatch.setenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, "true")
    settings = Settings(_env_file=None)

    assert settings.chili_phase5k_position_integrity_use_envelopes is True
    assert (
        position_integrity._position_integrity_source_relation(settings_=settings)
        == "trading_management_envelopes"
    )


def test_position_integrity_env_does_not_override_explicit_settings_object(monkeypatch):
    monkeypatch.setenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, "true")

    assert (
        position_integrity._position_integrity_source_relation(
            settings_=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=False)
        )
        == "trading_trades"
    )


def test_position_integrity_source_relation_defaults_to_app_settings(monkeypatch):
    monkeypatch.setattr(
        app_config,
        "settings",
        SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=True),
    )

    assert (
        position_integrity._position_integrity_source_relation()
        == "trading_management_envelopes"
    )


def test_position_integrity_reader_flag_has_no_direct_env_read():
    source = Path(position_integrity.__file__).read_text()

    assert "import os" not in source
    assert "os.environ" not in source
    assert "os.getenv" not in source


def test_position_integrity_source_relation_explicit_override(monkeypatch):
    monkeypatch.setenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, "true")

    assert (
        position_integrity._position_integrity_source_relation(
            use_envelopes=False,
            settings_=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=True),
        )
        == "trading_trades"
    )
    assert (
        position_integrity._position_integrity_source_relation(
            use_envelopes=True,
            settings_=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=False),
        )
        == "trading_management_envelopes"
    )


def test_audit_position_identity_uses_envelope_relation():
    db = _FakeDb()

    report = position_integrity.audit_position_identity(
        db,
        use_envelopes=True,
    )

    assert report.counts == {
        "open_positions_without_open_trade": 0,
        "open_trades_without_open_position": 0,
        "open_positions_missing_current_envelope": 0,
        "current_envelope_mismatches": 0,
        "repairable_current_envelope_links": 0,
    }
    joined = "\n".join(db.sqls)
    assert "trading_management_envelopes" in joined
    assert "trading_trades" not in joined
    assert len(db.sqls) == 5


def test_audit_position_identity_uses_typed_settings_relation():
    db = _FakeDb()

    report = position_integrity.audit_position_identity(
        db,
        settings_obj=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=True),
    )

    assert report.counts == {
        "open_positions_without_open_trade": 0,
        "open_trades_without_open_position": 0,
        "open_positions_missing_current_envelope": 0,
        "current_envelope_mismatches": 0,
        "repairable_current_envelope_links": 0,
    }
    joined = "\n".join(db.sqls)
    assert "trading_management_envelopes" in joined
    assert "trading_trades" not in joined


def test_repair_current_envelope_links_dry_run_uses_envelope_relation():
    db = _FakeDb()

    out = position_integrity.repair_current_envelope_links(
        db,
        dry_run=True,
        use_envelopes=True,
    )

    assert out["dry_run"] is True
    assert out["eligible"] == 0
    assert out["stale"] == 0
    joined = "\n".join(db.sqls)
    assert "trading_management_envelopes" in joined
    assert "trading_trades" not in joined
    assert len(db.sqls) == 2


def test_repair_current_envelope_links_env_does_not_override_typed_settings(monkeypatch):
    monkeypatch.setenv(position_integrity.PHASE5K_POSITION_INTEGRITY_ENV, "true")
    db = _FakeDb()

    out = position_integrity.repair_current_envelope_links(
        db,
        dry_run=True,
        settings_obj=SimpleNamespace(chili_phase5k_position_integrity_use_envelopes=False),
    )

    assert out["dry_run"] is True
    joined = "\n".join(db.sqls)
    assert "trading_trades" in joined
    assert "trading_management_envelopes" not in joined
