"""Ross's day-cushion risk ladder (2026-06-11 recap video).

"I am NOT taking full risk until I first have a cushion on the day" — risk
starts at half size, reaches normal once the day banks one base-loss, and
caps at 2x. A max-risk stop-out can never turn a >=1x-base-cushion day red.
"""

from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural.risk_policy import cushion_risk_multiplier


def _patch_day(monkeypatch, realized):
    import app.services.trading.governance as gov

    monkeypatch.setattr(
        gov, "global_realized_pnl_today_et",
        lambda db, user_id=None: {"total_usd": realized},
    )


def test_no_cushion_starts_at_full_base_risk(db, monkeypatch):
    # Floor raised 0.5 -> 1.0 (quant pass v2 A4): first triggers are the
    # highest-EV pool; the half-size start de-risked the day's best trades.
    _patch_day(monkeypatch, 0.0)
    mult, meta = cushion_risk_multiplier(db, base_loss_usd=50.0)
    assert mult == 1.0
    assert meta["cushion_usd"] == 0.0


def test_red_day_floors_at_full_base_risk(db, monkeypatch):
    _patch_day(monkeypatch, -120.0)
    mult, _ = cushion_risk_multiplier(db, base_loss_usd=50.0)
    assert mult == 1.0  # daily-loss cap + breaker bound the downside, not the floor


def test_one_base_cushion_restores_full_risk(db, monkeypatch):
    _patch_day(monkeypatch, 50.0)
    mult, _ = cushion_risk_multiplier(db, base_loss_usd=50.0)
    assert mult == 1.0


def test_big_cushion_caps_at_two_x(db, monkeypatch):
    _patch_day(monkeypatch, 500.0)
    mult, _ = cushion_risk_multiplier(db, base_loss_usd=50.0)
    assert mult == 2.0


def test_green_guarantee_above_one_base(db, monkeypatch):
    """Max-risk stop-out on a >=1x-base cushion leaves the day green
    (unchanged by the A4 floor: the ladder formula above 1x base is identical)."""
    for cushion in (50.0, 75.0, 150.0, 400.0):
        _patch_day(monkeypatch, cushion)
        mult, _ = cushion_risk_multiplier(db, base_loss_usd=50.0)
        assert cushion - mult * 50.0 >= 0.0


def test_fail_neutral_on_error(db, monkeypatch):
    import app.services.trading.governance as gov

    def _boom(db, user_id=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(gov, "global_realized_pnl_today_et", _boom)
    mult, meta = cushion_risk_multiplier(db, base_loss_usd=50.0)
    assert mult == 1.0
    assert meta["reason"] == "error_fail_neutral"
