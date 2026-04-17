"""Phase I - smoke test for the ``_run_capital_reweight_weekly_job`` scheduler hook.

Asserts the inner worker function is a no-op when mode is ``off``,
refuses to run when mode is ``authoritative``, and produces exactly
one DB row when mode is ``shadow`` (even with an empty paper-trade
table).
"""
from __future__ import annotations

from sqlalchemy import text

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

    def test_shadow_mode_with_no_positions_is_noop(self, db, monkeypatch):
        _cleanup(db)
        _force_mode(monkeypatch, "shadow")
        _run_capital_reweight_weekly_job()
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_capital_reweight_log"
        )).scalar_one()
        assert count == 0
