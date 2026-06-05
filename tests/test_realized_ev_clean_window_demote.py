"""Clean-window realized-EV demote pass.

The demote pass must judge a promoted pattern ONLY on its representative
post-floor (>= chili_realized_ev_clean_window_since) clean LIVE realized EV:

* pre-floor losses are IGNORED (not apples-to-apples — old execution regime),
* thin / short-span post-floor evidence is KEPT (data-starved supply, e.g. equity),
* dirty (reconcile/sync-gone) exits are excluded,
* only representative post-floor net-negative live evidence demotes,
* the settle window anchors on lifecycle_changed_at, not updated_at.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.models.core import User
from app.models.trading import ScanPattern, Trade

# Fixed absolute dates relative to the 2026-05-22 instrumentation floor so the
# tests are deterministic regardless of run date.
POST = datetime(2026, 5, 24)   # >= floor
PRE = datetime(2026, 5, 1)     # <  floor


def _user(db, name):
    u = User(name=name)
    db.add(u)
    db.flush()
    return u.id


def _promoted(db, name, *, settled=True):
    p = ScanPattern(name=name, timeframe="1d", rules_json={}, origin="mined", active=True)
    p.lifecycle_stage = "promoted"
    # past the settle window when settled=True (anchor = lifecycle_changed_at)
    p.lifecycle_changed_at = datetime(2026, 1, 1) if settled else datetime.utcnow()
    db.add(p)
    db.flush()
    return p


def _trade(uid, pid, *, pnl, exit_dt, exit_reason="target_hit"):
    return Trade(
        user_id=uid,
        scan_pattern_id=pid,
        ticker="BTC-USD",
        status="closed",
        direction="long",
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
        pnl=pnl,
        entry_date=exit_dt - timedelta(hours=2),
        exit_date=exit_dt,
        exit_reason=exit_reason,
        asset_kind="crypto",
    )


def _seed(db, uid, pid, *, pnls, base_dt, day_step=2, exit_reason="target_hit"):
    for i, pnl in enumerate(pnls):
        db.add(_trade(uid, pid, pnl=pnl, exit_dt=base_dt + timedelta(days=i * day_step), exit_reason=exit_reason))
    db.commit()


def _run(db):
    from app.services.trading.realized_ev_demote_pass import run_realized_ev_demote_pass
    return run_realized_ev_demote_pass(db)


def test_demote_on_representative_post_floor_net_negative(db):
    uid = _user(db, "d1")
    p = _promoted(db, "rep_neg")
    _seed(db, uid, p.id, pnls=[-3, -2, -4, -1, -5, -2], base_dt=POST, day_step=2)  # 6 trades, span 10d
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "challenged"
    assert summary["demoted_failing_gate"] == 1
    assert p.id in summary["demoted_pattern_ids"]


def test_kept_when_post_floor_evidence_thin(db):
    uid = _user(db, "d2")
    p = _promoted(db, "thin")
    _seed(db, uid, p.id, pnls=[-3, -2, -4], base_dt=POST, day_step=2)  # only 3 < min 5
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert summary["kept_unrepresentative_clean_window"] == 1
    assert summary["demoted_failing_gate"] == 0


def test_kept_when_span_too_short(db):
    uid = _user(db, "d3")
    p = _promoted(db, "burst")
    _seed(db, uid, p.id, pnls=[-3, -2, -4, -1, -5, -2], base_dt=POST, day_step=0)  # 6 trades, span 0d
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert summary["kept_unrepresentative_clean_window"] == 1


def test_pre_floor_losses_ignored(db):
    uid = _user(db, "d4")
    p = _promoted(db, "prefloor")
    _seed(db, uid, p.id, pnls=[-9, -8, -7, -6, -5, -4, -3, -2], base_dt=PRE, day_step=2)  # all pre-floor
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"  # NOT demoted on pre-floor churn
    assert summary["kept_unrepresentative_clean_window"] == 1


def test_not_demoted_when_post_floor_net_positive(db):
    uid = _user(db, "d5")
    p = _promoted(db, "rep_pos")
    _seed(db, uid, p.id, pnls=[3, 2, 4, 1, 5, 2], base_dt=POST, day_step=2)  # representative + positive
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert summary["kept_passing_gate"] == 1


def test_within_settle_window_kept(db):
    uid = _user(db, "d6")
    p = _promoted(db, "fresh", settled=False)  # lifecycle_changed_at = now
    _seed(db, uid, p.id, pnls=[-3, -2, -4, -1, -5, -2], base_dt=POST, day_step=2)
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert summary["kept_within_settle_window"] == 1


def test_dirty_post_floor_exits_excluded(db):
    uid = _user(db, "d7")
    p = _promoted(db, "dirty")
    _seed(db, uid, p.id, pnls=[-3, -2, -4, -1, -5, -2], base_dt=POST, day_step=2,
          exit_reason="coinbase_position_sync_gone")  # all dirty -> post-floor clean n=0
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert summary["kept_unrepresentative_clean_window"] == 1
