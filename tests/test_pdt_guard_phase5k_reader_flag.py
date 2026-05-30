from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import pdt_guard


class _FakeDb:
    def __init__(self) -> None:
        self.sql = ""
        self.params = None

    def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params or {}
        return self

    def fetchone(self):
        return SimpleNamespace(n=0)


def test_pdt_source_relation_defaults_to_compat_view(monkeypatch):
    monkeypatch.delenv(pdt_guard.PHASE5K_PDT_ENV, raising=False)

    assert pdt_guard._pdt_source_relation() == "trading_trades"


def test_pdt_source_relation_honors_env_flag(monkeypatch):
    monkeypatch.setenv(pdt_guard.PHASE5K_PDT_ENV, "true")

    assert pdt_guard._pdt_source_relation() == "trading_management_envelopes"


def test_pdt_source_relation_explicit_argument_overrides_env(monkeypatch):
    monkeypatch.setenv(pdt_guard.PHASE5K_PDT_ENV, "true")

    assert pdt_guard._pdt_source_relation(use_envelopes=False) == "trading_trades"
    assert (
        pdt_guard._pdt_source_relation(use_envelopes=True)
        == "trading_management_envelopes"
    )


def test_count_day_trades_default_reads_compat_view(monkeypatch):
    monkeypatch.delenv(pdt_guard.PHASE5K_PDT_ENV, raising=False)
    db = _FakeDb()

    assert pdt_guard._count_day_trades_5d(db) == 0

    assert "FROM trading_trades" in db.sql
    assert "FROM trading_management_envelopes" not in db.sql
    assert "reconcile_reasons" in db.params


def test_count_day_trades_flag_reads_management_envelopes():
    db = _FakeDb()

    assert pdt_guard._count_day_trades_5d(db, use_envelopes=True) == 0

    assert "FROM trading_management_envelopes" in db.sql
    assert "FROM trading_trades" not in db.sql
    assert "broker_order_id IS NOT NULL" in db.sql
    assert "last_fill_at IS NOT NULL" in db.sql
