"""Ang outcome broker-truth recon pass ay NAKA-SCHEDULE na sa lane (#1287).

ANG LANDMINE (2026-09-02). `risk_policy.load_current_live_loss_history` ay
nangangailangan, para sa bawat terminal Alpaca outcome na may fill, ng
`broker_recon_status == 'reconciled'` (+ broker pnl/notional). Kulang ⇒
`history_unavailable` ⇒ ang BUONG account ay `armed=0 skipped=
loss_guard_history_unavailable` sa bawat auto-arm pass. Ang `broker_*` na
column ay isinusulat lamang ng `outcome_reconcile` — na WALANG nag-i-schedule.
Ngayong araw: na-terminalize ng operator stop ang UPC 19457 (realized −48.97)
⇒ outcome 203719 NULL ang recon ⇒ JLHL (+40%, Ross 5 Pillars) hindi
na-arm nang 85 minuto (09:21–10:47Z). Ang manu-manong `reconcile_one_outcome`
ay nagbukas ng arming sa loob ng 8 segundo — kaya ang designed path ay
tumatakbo na ngayon kada 60s.

Runnable: pytest tests/test_outcome_recon_scheduled.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.services import trading_scheduler as ts
from app.services.trading.momentum_neural import outcome_reconcile as orc
from app.services.trading.momentum_neural import risk_policy as rp


def test_the_job_is_registered_next_to_auto_arm():
    """Positibong guard: naka-register ang job id sa scheduler wiring."""
    src = inspect.getsource(ts)
    assert 'id="momentum_outcome_broker_recon"' in src
    assert "_run_momentum_outcome_broker_recon_job" in src
    # sa parehong include_momentum_exec na sangay ng auto-arm
    reg = src.find('id="momentum_outcome_broker_recon"')
    assert "include_momentum_exec" in src[reg - 900: reg]


_ON = SimpleNamespace(
    chili_momentum_broker_truth_reconciliation_enabled=True,
    chili_momentum_live_runner_enabled=True,
    chili_momentum_outcome_recon_lookback_days=2.0,
)


def test_the_job_calls_the_designed_reconcile_pass_and_commits(monkeypatch):
    calls: list = []

    class _Db:
        def commit(self): calls.append("commit")
        def rollback(self): calls.append("rollback")
        def close(self): calls.append("close")

    monkeypatch.setattr("app.config.settings", _ON)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Db())
    monkeypatch.setattr(ts, "run_scheduler_job_guarded", lambda job_id, fn: (calls.append(job_id), fn()))
    monkeypatch.setattr(
        orc, "reconcile_momentum_outcomes_to_broker_truth",
        lambda db, *, lookback_days, day_net_advisory: (
            calls.append(("reconcile", lookback_days, day_net_advisory))
            or {"ok": True, "checked": 3, "written": 1, "by_status": {"reconciled": 1}}
        ),
    )
    ts._run_momentum_outcome_broker_recon_job()
    assert calls[0] == "momentum_outcome_broker_recon"
    assert ("reconcile", 2.0, False) in calls
    assert calls.index("commit") > calls.index(("reconcile", 2.0, False))
    assert "close" in calls and "rollback" not in calls


def test_the_flag_off_makes_the_job_a_no_op(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ts, "run_scheduler_job_guarded", lambda job_id, fn: fn())
    monkeypatch.setattr(
        "app.config.settings",
        SimpleNamespace(
            chili_momentum_broker_truth_reconciliation_enabled=False,
            chili_momentum_live_runner_enabled=True,
            chili_momentum_outcome_recon_lookback_days=2.0,
        ),
    )
    monkeypatch.setattr("app.db.SessionLocal", lambda: calls.append("db") or None)
    ts._run_momentum_outcome_broker_recon_job()
    assert calls == [], "walang DB session kapag patay ang flag"


def test_a_reconcile_failure_rolls_back_and_raises(monkeypatch):
    calls: list = []

    class _Db:
        def commit(self): calls.append("commit")
        def rollback(self): calls.append("rollback")
        def close(self): calls.append("close")

    monkeypatch.setattr("app.config.settings", _ON)
    monkeypatch.setattr("app.db.SessionLocal", lambda: _Db())
    monkeypatch.setattr(ts, "run_scheduler_job_guarded", lambda job_id, fn: fn())

    def _boom(db, **kw):
        raise RuntimeError("broker down")

    monkeypatch.setattr(orc, "reconcile_momentum_outcomes_to_broker_truth", _boom)
    try:
        ts._run_momentum_outcome_broker_recon_job()
    except RuntimeError:
        pass
    assert calls == ["rollback", "close"]


def test_why_it_matters_the_loss_guard_still_demands_reconciled():
    """Hindi binago ang guard: 'reconciled' + petsa + finite pnl + positibong
    notional. Ang job ang naglalagay nito; hindi ito nagpapaluwag ng bantay."""
    ok = SimpleNamespace(
        broker_recon_status="reconciled", broker_reconciled_at=__import__("datetime").datetime(2026, 9, 2),
        broker_realized_pnl_usd=-48.97, broker_notional_basis_usd=1694.33,
    )
    assert rp._alpaca_loss_history_broker_truth(ok) is True
    unreconciled = SimpleNamespace(
        broker_recon_status=None, broker_reconciled_at=None,
        broker_realized_pnl_usd=None, broker_notional_basis_usd=None,
    )
    assert rp._alpaca_loss_history_broker_truth(unreconciled) is False


def test_ships_on():
    s = Settings()
    assert s.chili_momentum_broker_truth_reconciliation_enabled is True
    assert s.chili_momentum_outcome_recon_lookback_days == 2.0
