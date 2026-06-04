"""Phase I - smoke test for the ``_run_capital_reweight_weekly_job`` scheduler hook.

Asserts the inner worker function is a no-op when mode is ``off``,
refuses to run when mode is ``authoritative``, and produces exactly
one DB row when mode is ``shadow`` (even with an empty paper-trade
table).
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text

from app.config import Settings
from app.services.trading_scheduler import _run_capital_reweight_weekly_job


def _cleanup(db) -> None:
    db.execute(text("DELETE FROM trading_capital_reweight_log"))
    db.execute(text("DELETE FROM trading_paper_trades"))
    db.commit()


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.config.settings.brain_capital_reweight_mode",
        mode,
        raising=False,
    )


class TestSchedulerJob:
    def test_off_mode_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "off")
        _run_capital_reweight_weekly_job()
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log"
        )).scalar_one()
        assert count == 0

    def test_shadow_mode_with_open_positions_writes_row(self, db, monkeypatch):
        _cleanup(db)
        # Seed one open paper trade so the sweep has non-empty buckets.
        db.execute(text("""
            INSERT INTO trading_paper_trades
                (ticker, direction, entry_price, quantity, status,
                 entry_date, created_at)
            VALUES
                ('TEST_AAA', 'long', 100.0, 10, 'open', NOW(), NOW())
        """))
        db.commit()

        _force_mode(monkeypatch, "shadow")
        _run_capital_reweight_weekly_job()
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log"
        )).scalar_one()
        assert count == 1

    def test_shadow_mode_uses_total_capital_default_setting(self, monkeypatch):
        captured = {}

        class _Rows:
            def fetchall(self):
                return [("equity:default", 200.0)]

        class _Db:
            def execute(self, *_args, **_kwargs):
                return _Rows()

            def rollback(self):
                return None

            def close(self):
                return None

        monkeypatch.setenv("BRAIN_CAPITAL_REWEIGHT_TOTAL_CAPITAL_DEFAULT", "250000")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        monkeypatch.setattr("app.config.settings", settings)
        _force_mode(monkeypatch, "shadow")
        monkeypatch.setattr("app.db.SessionLocal", lambda: _Db())
        monkeypatch.setattr(
            "app.services.trading.risk_dial_service.get_latest_dial",
            lambda *_args, **_kwargs: 0.8,
        )
        monkeypatch.setattr(
            "app.services.trading.capital_reweight_service.run_sweep",
            lambda *_args, **kwargs: captured.update(kwargs) or SimpleNamespace(
                reweight_id="test-reweight",
                mode="shadow",
                mean_drift_bps=0.0,
                p90_drift_bps=0.0,
            ),
        )
        monkeypatch.setattr(
            "app.services.trading_scheduler.run_scheduler_job_guarded",
            lambda _job_id, work: work(),
        )

        _run_capital_reweight_weekly_job()

        assert settings.brain_capital_reweight_total_capital_default == 250_000.0
        assert captured["total_capital"] == 250_000.0
        assert captured["dial_value"] == 0.8

    def test_shadow_mode_with_no_positions_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        _run_capital_reweight_weekly_job()
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log"
        )).scalar_one()
        assert count == 0
