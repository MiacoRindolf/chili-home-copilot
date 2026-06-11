"""Spread-stability gate + kill-switch resilience (the 2026-06-11 INDP lessons):
one clean BBO instant inside a flickering spread regime passed the gate (the
MEDIAN of the recent tape is the market); the daily-loss cap must be adaptive
(pct-of-equity, fail-closed floor) and self-clear at the ET day roll; and the
exit-retry broker-zero reconcile must cover Robinhood, not just Coinbase.

(A midday RVOL tier was built and FALSIFIED by the 06-10/06-11 replay A/B —
midday losers were high-volume names; volume is not the midday discriminator —
so it was removed. Spread stability covers the INDP class.)"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.trading.momentum_neural.nbbo_tape import recent_spread_median_bps


def _ensure_table(db: Session) -> None:
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS momentum_nbbo_spread_tape ("
        " id BIGSERIAL PRIMARY KEY, symbol VARCHAR(32) NOT NULL,"
        " observed_at TIMESTAMPTZ NOT NULL DEFAULT now(), bid DOUBLE PRECISION,"
        " ask DOUBLE PRECISION, mid DOUBLE PRECISION, spread_bps DOUBLE PRECISION,"
        " day_volume DOUBLE PRECISION, source VARCHAR(24) NOT NULL DEFAULT 'massive_snapshot')"
    ))
    db.execute(text("DELETE FROM momentum_nbbo_spread_tape"))
    db.commit()


# ── spread stability ─────────────────────────────────────────────────────────
def test_spread_median_reads_recent_window(db: Session) -> None:
    _ensure_table(db)
    now = datetime.now(timezone.utc)
    rows = [(now - timedelta(seconds=s), bps) for s, bps in
            ((5, 120.0), (15, 110.0), (25, 95.0), (35, 130.0), (45, 105.0),
             (3000, 10.0))]  # the old quiet row is OUTSIDE the window
    for at, bps in rows:
        db.execute(text(
            "INSERT INTO momentum_nbbo_spread_tape (symbol, observed_at, spread_bps) "
            "VALUES ('INDP', :at, :b)"), {"at": at.replace(tzinfo=None), "b": bps})
    db.commit()
    out = recent_spread_median_bps(db, "INDP", window_s=60.0, now_utc=now)
    assert out is not None
    median, n = out
    assert n == 5
    assert median == 110.0  # the flickering regime's median, not one lucky tick


def test_spread_median_none_on_empty(db: Session) -> None:
    _ensure_table(db)
    assert recent_spread_median_bps(db, "GHOST", window_s=60.0) is None


# ── adaptive daily-loss cap ──────────────────────────────────────────────────
def test_daily_loss_cap_uses_pct_of_resolved_equity(db: Session, monkeypatch) -> None:
    import app.services.trading.governance as gov
    from app.config import settings

    monkeypatch.setattr(settings, "chili_global_max_daily_loss_usd", 0.0, raising=False)
    monkeypatch.setattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.015, raising=False)
    monkeypatch.setattr(
        gov, "global_realized_pnl_today_et",
        lambda *_a, **_k: {"total_usd": -100.0, "autotrader_usd": 0.0, "momentum_usd": -100.0},
    )
    import app.services.trading.momentum_neural.risk_policy as rp

    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: 22_500.0)
    out = gov.check_daily_loss_breach(db, user_id=1, activate=False)
    assert out["source"] == "pct_equity"
    assert abs(out["limit_usd"] - 337.5) < 1e-6  # 1.5% x 22.5k — adaptive
    assert out["breached"] is False


def test_daily_loss_cap_fails_closed_when_equity_unresolvable(db: Session, monkeypatch) -> None:
    import app.services.trading.governance as gov
    from app.config import settings

    monkeypatch.setattr(settings, "chili_global_max_daily_loss_usd", 0.0, raising=False)
    monkeypatch.setattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.015, raising=False)
    monkeypatch.setattr(
        gov, "global_realized_pnl_today_et",
        lambda *_a, **_k: {"total_usd": -500.0, "autotrader_usd": 0.0, "momentum_usd": -500.0},
    )
    import app.services.trading.momentum_neural.risk_policy as rp

    monkeypatch.setattr(rp, "_account_equity_usd", lambda *a, **k: None)
    out = gov.check_daily_loss_breach(db, user_id=1, activate=False)
    assert out["source"] == "usd_failsafe"  # never uncapped
    assert out["limit_usd"] == 300.0
    assert out["breached"] is True


# ── daily-breach auto-clear at ET day roll ───────────────────────────────────
def test_daily_breach_auto_clears_next_et_day(monkeypatch) -> None:
    import app.services.trading.governance as gov

    monkeypatch.setattr(gov, "_persist_kill_switch_state", lambda *a, **k: True)
    monkeypatch.setattr(gov, "_refresh_kill_switch_from_db_if_due", lambda *a, **k: None)
    gov.activate_kill_switch("global_daily_loss_breach_pct_equity_$338")
    # same-day: stays active
    gov._auto_clear_stale_daily_breach()
    assert gov.get_kill_switch_status()["active"] is True
    # roll the set_at back one ET day -> self-clears
    with gov._kill_switch_lock:
        gov._kill_switch_set_at = datetime.utcnow() - timedelta(days=1)
    gov._auto_clear_stale_daily_breach()
    assert gov.get_kill_switch_status()["active"] is False


def test_manual_kill_switch_never_auto_clears(monkeypatch) -> None:
    import app.services.trading.governance as gov

    monkeypatch.setattr(gov, "_persist_kill_switch_state", lambda *a, **k: True)
    monkeypatch.setattr(gov, "_refresh_kill_switch_from_db_if_due", lambda *a, **k: None)
    gov.activate_kill_switch("manual")
    with gov._kill_switch_lock:
        gov._kill_switch_set_at = datetime.utcnow() - timedelta(days=3)
    gov._auto_clear_stale_daily_breach()
    assert gov.get_kill_switch_status()["active"] is True
    gov.deactivate_kill_switch()  # cleanup


# ── family-agnostic broker-zero reconcile ────────────────────────────────────
def test_rh_position_zero_reconcile(monkeypatch) -> None:
    from app.services.trading.momentum_neural import live_runner as lr

    sess = SimpleNamespace(execution_family="robinhood_spot", symbol="INDP")
    import app.services.broker_service as bs

    monkeypatch.setattr(bs, "get_open_position_quantity", lambda t: 0.0)
    assert lr._broker_position_confirms_zero(sess) is True
    monkeypatch.setattr(bs, "get_open_position_quantity", lambda t: 612.0)
    assert lr._broker_position_confirms_zero(sess) is False
    monkeypatch.setattr(bs, "get_open_position_quantity", lambda t: None)
    assert lr._broker_position_confirms_zero(sess) is False  # unknown != flat
