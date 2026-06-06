"""Realized-PnL promotion pass: graduate not-yet-promoted patterns that prove
themselves on clean realized PnL (passing the realized-EV gate + a meaningful
realized-edge floor), ranked by realized average return, capped per run, and
gated by the kill switch.
"""
from __future__ import annotations

from app.models.trading import ScanPattern


def _pat(db, name, *, stage="challenged", raw_avg=None, raw_wr=None, raw_n=None,
         corrected_avg=None, corrected_wr=None, corrected_n=None, active=True):
    p = ScanPattern(name=name, timeframe="1d", rules_json={}, origin="mined", active=active)
    p.lifecycle_stage = stage
    p.raw_realized_avg_return_pct = raw_avg
    p.raw_realized_win_rate = raw_wr
    p.raw_realized_trade_count = raw_n
    p.corrected_avg_return_pct = corrected_avg
    p.corrected_win_rate = corrected_wr
    p.corrected_trade_count = corrected_n
    db.add(p)
    db.flush()
    return p


def _run(db):
    from app.services.trading.realized_pnl_promotion import run_realized_pnl_promotion_pass
    return run_realized_pnl_promotion_pass(db)


def test_promotes_clean_realized_winner(db):
    # challenged, +3.35% over 10 clean trades -> graduates on realized PnL.
    p = _pat(db, "winner", stage="challenged", raw_avg=3.35, raw_wr=0.6, raw_n=10)
    db.commit()
    summary = _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "promoted"
    assert p.promotion_status == "promoted_via_realized_pnl"
    assert p.id in summary["promoted_pattern_ids"]


def test_skips_thin_sample(db):
    p = _pat(db, "thin", stage="challenged", raw_avg=3.0, raw_wr=0.6, raw_n=5)  # n<8
    db.commit()
    _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "challenged"


def test_skips_marginal_edge(db):
    p = _pat(db, "marginal", stage="challenged", raw_avg=0.1, raw_wr=0.6, raw_n=15)  # avg<0.5
    db.commit()
    _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "challenged"


def test_skips_net_negative(db):
    p = _pat(db, "loser", stage="challenged", raw_avg=-1.0, raw_wr=0.3, raw_n=12)
    db.commit()
    _run(db)
    db.refresh(p)
    assert p.lifecycle_stage == "challenged"


def test_does_not_touch_already_promoted_or_ladder(db):
    a = _pat(db, "already", stage="promoted", raw_avg=3.0, raw_wr=0.6, raw_n=10)
    b = _pat(db, "ladder", stage="shadow_promoted", raw_avg=3.0, raw_wr=0.6, raw_n=10)
    db.commit()
    summary = _run(db)
    assert a.id not in summary["promoted_pattern_ids"]
    assert b.id not in summary["promoted_pattern_ids"]


def test_ranks_by_realized_pnl_and_caps(db, monkeypatch):
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "chili_realized_pnl_promotion_max_per_run", 2, raising=False)
    p_lo = _pat(db, "lo", raw_avg=0.8, raw_wr=0.6, raw_n=10)
    p_hi = _pat(db, "hi", raw_avg=5.0, raw_wr=0.7, raw_n=10)
    p_mid = _pat(db, "mid", raw_avg=2.0, raw_wr=0.6, raw_n=10)
    db.commit()
    summary = _run(db)
    # capped at 2, ranked by realized avg desc -> hi, mid promoted; lo not.
    assert summary["promoted"] == 2
    assert summary["promoted_details"][0]["id"] == p_hi.id
    db.refresh(p_lo)
    assert p_lo.lifecycle_stage == "challenged"


def test_kill_switch_skips(db, monkeypatch):
    import app.services.trading.governance as gov
    monkeypatch.setattr(gov, "is_kill_switch_active", lambda: True)
    p = _pat(db, "killsw", raw_avg=3.35, raw_wr=0.6, raw_n=10)
    db.commit()
    summary = _run(db)
    db.refresh(p)
    assert summary["skipped_kill_switch"] is True
    assert p.lifecycle_stage == "challenged"
